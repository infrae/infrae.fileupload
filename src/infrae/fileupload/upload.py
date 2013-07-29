# -*- coding: utf-8 -*-

import cgi
import fcntl
import io
import json
import logging
import os
import re
import shutil

from webob import Request, Response, exc

__all__ = ['make_app']
logger = logging.getLogger('infrae.fileupload')
VALID_ID = re.compile(r'^[a-zA-Z0-9=-]*$')


class Lock(object):
    """Lock a code-path based on a file on the filesystem.
    """

    def __init__(self, filename):
        self._filename = filename
        self._opened = None

    def __enter__(self):
        try:
            self._opened = open(self._filename, 'wb')
            fcntl.flock(self._opened, fcntl.LOCK_EX)
        except IOError:
            raise ValueError('Cannot lock upload directory')

    def __exit__(self, exc_type, exc_val, exc_tb):
        fcntl.flock(self._opened, fcntl.LOCK_UN)
        self._opened.close()
        self._opened = None


class UploadManager(object):
    """Manage the upload directory.
    """

    def __init__(self, directory):
        self._directory = directory
        self._lock = os.path.join(directory, 'upload.lock')

    def _get_lock(self):
        return Lock(self._lock)

    def _get_upload_bucket(self, create, identifier, factory):
        path = os.path.join(self._directory, identifier)
        with self._get_lock():
            if not os.path.isdir(path):
                if create:
                    os.makedirs(path)
                else:
                    return None
            return factory(self, identifier, path)

    def create_upload_bucket(self, identifier, *args):
        return self._get_upload_bucket(
            True,
            identifier,
            lambda api, identifier, path: UploadFileBucket(
                api, identifier, path, *args))

    def access_upload_bucket(self, identifier):
        return self._get_upload_bucket(False, identifier, FileBucket)

    def clear_upload_bucket(self, identifier):
        path = os.path.join(self._directory, identifier)
        with self._get_lock():
            if os.path.isdir(path):
                shutil.rmtree(path)


class FileBucket(object):
    """Give access to a bucket containing uploaded files.
    """

    def __init__(self, api, identifier, directory):
        self._metadata = os.path.join(directory, 'metadata.json')
        self._done = os.path.join(directory, 'uploaded.json')
        self._data = os.path.join(directory, 'data.bin')
        self._identifier = identifier
        self._api = api
        self._status = None

    def get_status(self):
        if self._status is not None:
            return self._status
        status = None
        with open(self._metadata, 'rb') as stream:
            try:
                data = json.loads(stream.read())
                if isinstance(data, dict):
                    status = data
            except:
                pass
        if status is not None and 'uploaded-finished' not in status:
            with open(self._done, 'rb') as stream:
                try:
                    done = int(stream.read())
                except:
                    done = 0
                status['uploaded-length'] = done
        self._status = status
        return status

    def get_filename(self):
        if self.is_complete():
            return self._data
        return None

    def clean(self):
        # This is called by the middleware if the upload fails.
        self._api.clear_upload_bucket(self._identifier)

    def is_complete(self):
        status = self.get_status()
        if status is not None:
            return status.get('upload-finished', False)
        return False


def open_data(filename, payload=None):
    """Open a filename and write a payload in it, flush it and return
    the file object for futher write operaions in it. If the file
    already exists it will deleted and recreated. This is needed so
    that if an another process was writing in the file, the content
    written by this one won't be overriden. This should be called only
    when the directory is locked.
    """
    if os.path.isfile(filename):
        os.unlink(filename)
    descriptor = open(filename, 'wb')
    if payload:
        descriptor.write(payload)
    descriptor.flush()
    return descriptor


class UploadFileBucket(FileBucket):
    """Upload a new file (and manage it) inside a bucket.
    """

    def __init__(self, api, identifier, directory,
                 filename, content_type, content_length):
        super(UploadFileBucket, self).__init__(api, identifier, directory)
        self._length = content_length
        self._metadata_descriptor = open_data(
            self._metadata,
            json.dumps({'identifier': identifier,
                        'filename': filename,
                        'content-type': content_type,
                        'request-length': content_length}))
        self._done_descriptor = open_data(self._done, '0')
        self._data_descriptor = open_data(self._data)

    def metadata(self, finished=False, error=None):
        status = self.get_status()
        if finished:
            status['upload-finished'] = True
        if error:
            status['upload-error'] = error
        self._metadata_descriptor.seek(0)
        self._metadata_descriptor.write(json.dumps(status))
        self._metadata_descriptor.flush()
        self._status = None

    def progress(self):
        total = 0
        while True:
            try:
                read = yield
            except GeneratorExit:
                # Reload and save metadata.
                self.metadata(finished=(total == self._length))
                self._metadata_descriptor.close()
                self._done_descriptor.close()
                self._status = None
                raise StopIteration
            total += read
            self._done_descriptor.seek(0)
            self._done_descriptor.write(str(total))
            self._done_descriptor.flush()

    def write(self):
        while True:
            try:
                block = yield
            except GeneratorExit:
                self._data_descriptor.close()
                raise StopIteration
            self._data_descriptor.write(block)

BLOCK_SIZE = 16 * 1024 * 1024
EOL = '\r\n'

class Reader(object):

    def __init__(self, stream, length):
        self._to_read = length
        self._read = 0
        self._stream = stream
        self._notifies = [self._track]

    def _track(self, read):
        self._read += read
        self._to_read -= read

    def subscribe(self, notifier):
        notifier(self._read)
        self._notifies.append(notifier)

    def notify(self, read):
        for notifier in self._notifies:
            notifier(read)

    def read(self):
        max_size = BLOCK_SIZE if BLOCK_SIZE < self._to_read else self._to_read
        if max_size:
            data = self._stream.readline(max_size)
            self.notify(len(data))
            return data
        return None


class UploadMiddleware(object):
    """A middleware class to handle POST data and get stats on the
    file upload progress.
    """

    def __init__(self, application, directory, max_size=None):
        self.application = application
        self.manager = UploadManager(directory)
        self.max_size = max_size

    def __call__(self, environ, start_response):
        request = Request(environ)
        application = self.application

        # XXX We need a better test here.
        if request.path_info.endswith('/upload'):
            identifier = request.GET.get('identifier')

            if identifier is None or not VALID_ID.match(identifier):
                logger.error('Malformed upload identifier "%s"', identifier)
                application = exc.HTTPServerError('Malformed upload identifier')
            else:
                if 'status' in request.GET:
                    application = self.status(request, identifier)
                elif (request.method == 'POST' and
                      request.content_type == 'multipart/form-data'):
                    application = self.upload(request, identifier)

        environ['infrae.fileupload.manager'] = self.manager
        return application(environ, start_response)

    def upload(self, request, identifier):
        """Handle upload of a file.
        """
        upload = None

        def fail(error):
            if upload is None:
                failed = self.manager.create_upload_bucket(
                    identifier, '', 'n/a', '0')
            else:
                failed = upload
            failed.metadata(error=error)
            return exc.HTTPServerError(error)

        length = request.content_length
        if self.max_size and int(length) > self.max_size:
            return fail('Upload is too large')

        _, options = cgi.parse_header(request.headers['content-type'])
        if 'boundary' not in options:
            return fail('Upload request is malformed #1')

        part_boundary = '--' + options['boundary'] + EOL
        end_boundary = '--' + options['boundary'] + '--' + EOL

        input_stream = Reader(request.environ['wsgi.input'], length)
        # Read the first marker
        marker = input_stream.read()
        if marker != part_boundary:
            return fail('Upload request is malformed #2')

        # Read the headers
        headers = {}
        line = input_stream.read()
        while line != EOL:
            name, payload = line.split(':', 1)
            headers[name.lower().strip()] = cgi.parse_header(payload)
            line = input_stream.read()

        # We should have now the payload
        if ('content-disposition' not in headers or
            'content-type' not in headers or
            headers['content-disposition'][0] != 'form-data' or
            not headers['content-disposition'][1].get('filename')):
            return fail('Upload request is malformed #3')

        upload = self.manager.create_upload_bucket(
            identifier,
            headers['content-disposition'][1]['filename'],
            headers['content-type'][0],
            length)
        track_progress = upload.progress()
        track_progress.send(None)
        input_stream.subscribe(track_progress.send)
        request.environ['infrae.fileupload.current'] = upload
        line = None
        output_stream = upload.write()
        while line != end_boundary:
            output_stream.send(line)
            line = input_stream.read()
            if line == part_boundary:
                # Multipart, we don't handle that
                error = fail('Upload request is malformed #4')
                output_stream.close()
                track_progress.close()
                return error

        # We are done uploading
        output_stream.close()
        track_progress.close()

        # Get the response from the application
        info = json.dumps(upload.get_status())
        request.environ['wsgi.input'] = io.BytesIO(info)
        request.content_type = 'application/json'
        request.content_length = len(info)
        return request.get_response(self.application)

    def status(self, request, identifier):
        """Handle status information on the upload process of a file.
        """
        result = {'missing': True}
        upload = self.manager.access_upload_bucket(identifier)
        if upload is not None:
            status = upload.get_status()
            if status is not None:
                result = status

        logger.info('%s: %s', identifier, result)
        response = Response()
        response.content_type = 'application/json'
        if 'callback' in request.GET:
            response.body = request.GET['callback'] + '(' + json.dumps(result) + ')'
        else:
            response.body = json.dumps(result)
        return response


def make_app(application, global_conf, directory, max_size=0):
    """build a FileUpload application
    """
    directory = os.path.normpath(directory)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    logger.info('Upload directory: %s' % directory)

    if max_size:
        # use Mo
        max_size = int(max_size)*1024*1024
        logger.info('Max upload size: %s' % max_size)

    return UploadMiddleware(application, directory, max_size=max_size)
