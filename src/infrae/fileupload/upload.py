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

    def __init__(self, directory):
        self._directory = directory
        self._lock = os.path.join(directory, 'upload.lock')

    def _get_lock(self):
        return Lock(self._lock)

    def _get_upload_file(self, create, identifier, factory):
        path = os.path.join(self._directory, identifier)
        with self._get_lock():
            if not os.path.isdir(path):
                if create:
                    os.makedirs(path)
                else:
                    return None
            return factory(self, identifier, path)

    def create_upload_file(self, identifier, *args):
        return self._get_upload_file(
            True,
            identifier,
            lambda api, identifier, path: UploadingFile(
                api, identifier, path, *args))

    def access_upload_file(self, identifier):
        return self._get_upload_file(False, identifier, UploadFile)

    def clear_upload_file(self, identifier):
        path = os.path.join(self._directory, identifier)
        with self._get_lock():
            if os.path.isdir(path):
                shutil.rmtree(path)


class UploadFile(object):

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
                if done == status['request-length']:
                    status['upload-finished'] = True
        self._status = status
        return status

    def get_filename(self):
        if self.is_complete():
            return self._data
        return None

    def clean(self):
        # This is called by the middleware if the upload fails.
        self._api.clear_upload_file(self._identifier)

    def is_complete(self):
        status = self.get_status()
        if status is not None:
            return status.get('upload-finished', False)
        return False


class UploadingFile(UploadFile):

    def __init__(self, api, identifier, directory,
                 filename, content_type, content_length):
        super(UploadingFile, self).__init__(api, identifier, directory)
        self._length = content_length
        with open(self._metadata, 'wb') as stream:
            stream.write(json.dumps({'filename': filename,
                                     'content-type': content_type,
                                     'request-length': content_length}))
        with open(self._done, 'wb') as stream:
            stream.write('0')

    def progress(self):
        total = 0
        with open(self._done, 'wb') as done:
            while True:
                try:
                    read = yield
                except GeneratorExit:
                    # Reload and save metadata.
                    status = json.dumps(self.get_status())
                    with open(self._metadata, 'wb') as stream:
                        stream.write(status)
                    raise StopIteration
                total += read
                done.seek(0)
                done.write(str(total))
                done.flush()

    def write(self):
        with open(self._data, 'wb') as data:
            while True:
                try:
                    block = yield
                except GeneratorExit:
                    raise StopIteration
                data.write(block)

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

        if request.path_info.endswith('/++rest++silva.upload'):
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
        length = request.content_length
        if self.max_size and int(length) > self.max_size:
            return exc.HTTPServerError('Upload is too large')

        _, options = cgi.parse_header(request.headers['content-type'])
        if 'boundary' not in options:
            return exc.HTTPServerError(
                'Upload request is malformed (boundary missing)')

        part_boundary = '--' + options['boundary'] + EOL
        end_boundary = '--' + options['boundary'] + '--' + EOL

        input_stream = Reader(request.environ['wsgi.input'], length)
        # Read the first marker
        marker = input_stream.read()
        if marker != part_boundary:
            return exc.HTTPServerError(
                'Upload request is malformed (invalid construction)')

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
            headers['content-disposition'][0] != 'form-data'):
            return exc.HTTPServerError(
                'Upload request is malformed (missing payload)')

        upload = self.manager.create_upload_file(
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
                upload.clean()
                return exc.HTTPServerError(
                    'Upload request is malformed (two fields)')

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
        """Handle status information on the upload process of a file
        """
        result = {'missing': True}
        upload = self.manager.access_upload_file(identifier)
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
        return result


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
