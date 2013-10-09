# -*- coding: utf-8 -*-

import cgi
import fcntl
import hmac
import io
import json
import logging
import os
import re
import shutil
import uuid

from webob import Request, Response, exc

logger = logging.getLogger('infrae.fileupload')
VALID_ID = re.compile(r'^[a-zA-Z0-9:-]*$')
IDENTIFIER_KEY = 'X-Progress-ID'
BLOCK_SIZE = 16 * 1024 * 1024


def compare(original, tested=''):
    """Compare two lines together, paying attention to the different
    possibilities to end lines.

    >>> compare(None)
    False
    >>> compare('\\n')
    True
    >>> compare('\\r\\n')
    True
    >>> compare('foo\\r\\n')
    False
    >>> compare(None, 'foo')
    False
    >>> compare('foo', 'bar')
    False
    >>> compare('foo', 'foo')
    False
    >>> compare('foo\\n', 'foo')
    True
    >>> compare('foo\\r\\n', 'foo')
    True
    >>> compare('foo\\t\\n', 'foo')
    False
    """
    if original and original.startswith(tested):
        if original[len(tested):] in ('', '\r', '\n', '\r\n'):
            return True
    return False


class UploadError(ValueError):
    msg = 'Unknown upload error'


class InvalidIdentifierError(UploadError):
    msg = 'Provided upload identifier was invalid'


class RequestError(UploadError):

    def __init__(self, msg):
        self.msg = msg


class NetworkError(UploadError):
    msg = 'Network error, cannot read upload'


class UploadDirectoryError(UploadError):
    msg = 'Server configuration error, upload directory is missing'


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
            raise UploadDirectoryError()

    def __exit__(self, exc_type, exc_val, exc_tb):
        fcntl.flock(self._opened, fcntl.LOCK_UN)
        self._opened.close()
        self._opened = None


class UploadManager(object):
    """Manage the upload directory.
    """

    def __init__(self, directory, upload_url=None, upload_key=None):
        self._key = upload_key
        self._directory = directory
        self._lock = os.path.join(directory, 'upload.lock')
        self._upload_url = upload_url

    @property
    def upload_url(self):
        return self._upload_url

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

    def _check_identifier(self, identifier):
        if identifier is None and not VALID_ID.match(identifier):
            return None
        if ':' in identifier:
            identifier, user_key = identifier.split(':', 1)
            if self._key:
                expected_key = hmac.new(self._key, identifier).hexdigest()
                if user_key == expected_key:
                    return identifier
            # There was a key but non was configured.
            return None
        else:
            if self._key:
                # Key was provided but not present in user input.
                return None
        return identifier

    def verify_identifier(self, identifier):
        return self._check_identifier(identifier) != None

    def create_identifier(self):
        identifier = str(uuid.uuid1())
        if self._key:
            identifier += ':' + hmac.new(self._key, identifier).hexdigest()
        return identifier

    def create_upload_bucket(self, identifier, *args):
        identifier = self._check_identifier(identifier)
        if identifier is None:
            raise InvalidIdentifierError()
        return self._get_upload_bucket(
            True,
            identifier,
            lambda api, identifier, path: UploadFileBucket(
                api, identifier, path, *args))

    def access_upload_bucket(self, identifier):
        identifier = self._check_identifier(identifier)
        if identifier is None:
            raise InvalidIdentifierError()
        return self._get_upload_bucket(False, identifier, FileBucket)

    def clear_upload_bucket(self, identifier):
        identifier = self._check_identifier(identifier)
        if identifier is None:
            raise InvalidIdentifierError()
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

    def get_status(self, refresh=False):
        """Return the status of the upload.
        """
        if not refresh and self._status is not None:
            return self._status
        status = None
        with open(self._metadata, 'rb') as stream:
            try:
                data = json.loads(stream.read())
                if isinstance(data, dict):
                    status = data
            except:
                pass
        if status is not None and 'received' not in status:
            with open(self._done, 'rb') as stream:
                try:
                    done = int(stream.read())
                except:
                    done = 0
                status['received'] = done
                status['state'] = 'uploading'
        self._status = status
        return status

    def get_filename(self):
        """Return the filename of the upload.
        """
        if self.is_complete():
            return self._data
        return None

    def clear_upload(self):
        # This is called by the middleware if the upload fails.
        path = os.path.join(self._api._directory, self._identifier)
        with self._api._get_lock():
            if os.path.isdir(path):
                shutil.rmtree(path)

    def is_complete(self):
        """Return true if the upload is done.
        """
        status = self.get_status(refresh=True)
        if status is not None:
            return status.get('state', 'unknown') == 'done'
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
    descriptor.flush()          # Make sure the file is created on the FS
    return descriptor


class UploadFileBucket(FileBucket):
    """Upload a new file (and manage it) inside a bucket.
    """

    def __init__(self, api, identifier, directory,
                 filename, content_type, content_length):
        super(UploadFileBucket, self).__init__(api, identifier, directory)
        self._finished = False
        self._length = content_length
        self._metadata_descriptor = open_data(
            self._metadata,
            json.dumps({'identifier': identifier,
                        'filename': filename,
                        'content-type': content_type,
                        'state': 'starting',
                        'size': content_length}))
        self._done_descriptor = open_data(self._done, '0')
        self._data_descriptor = open_data(self._data)

    def finish_upload(self, error=None):
        if self._metadata_descriptor is None:
            logger.error('Error while closing the upload (already closed).')
            return
        status = self.get_status(refresh=True)
        if error:
            status['state'] = 'error'
            status['error'] = error
        elif self._finished:
            status['state'] = 'done'
        self._metadata_descriptor.seek(0)
        self._metadata_descriptor.write(json.dumps(status))
        self._metadata_descriptor.flush()
        self._metadata_descriptor.close()
        self._metadata_descriptor = None
        self._status = None

    def progress(self):
        total = 0
        while True:
            try:
                read = yield
            except GeneratorExit:
                # Reload and save metadata.
                self._done_descriptor.close()
                self._done_descriptor = None
                raise StopIteration
            total += read
            self._finished = total == self._length
            self._done_descriptor.seek(0)
            self._done_descriptor.write(str(total))
            self._done_descriptor.flush()

    def write(self):
        while True:
            try:
                block = yield
            except GeneratorExit:
                self._data_descriptor.close()
                self._data_descriptor = None
                raise StopIteration
            if isinstance(block, str):
                self._data_descriptor.write(block)
            else:
                logger.error('Received invalid data to write: %r', block)


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

    def read(self, line=False):
        data = ''
        need_more = True
        while need_more:
            # It is possible we didn't get a full line, and wsgi.input
            # is not blocking.
            max_size = min(BLOCK_SIZE, self._to_read)
            if max_size:
                try:
                    data += self._stream.readline(max_size)
                except IOError:
                    raise NetworkError()
                self.notify(len(data))
                need_more = line and data[-1] != '\n'
            else:
                need_more = False
        return data or None


class UploadMiddleware(object):
    """A middleware class to handle POST data and get stats on the
    file upload progress.
    """

    def __init__(self, application, directory, max_size=None,
             upload_url=None, upload_key=None):
        self.application = application
        self.manager = UploadManager(
            directory, upload_url=upload_url, upload_key=upload_key)
        self.max_size = max_size

    def __call__(self, environ, start_response):
        application = self.application

        if not self.manager.upload_url:
            # XXX We need a better test here.
            request = Request(environ)
            if request.path_info.endswith('/upload'):
                identifier = request.GET.get(IDENTIFIER_KEY)

                if not self.manager.verify_identifier(identifier):
                    logger.error('Malformed upload identifier "%s"', identifier)
                    application = exc.HTTPServerError(
                        'Malformed upload identifier')
                else:
                    if 'status' in request.GET:
                        application = self.status(request, identifier)
                    elif 'clear' in request.GET:
                        application = self.clear(request, identifier)
                    elif (request.method == 'POST' and
                          request.content_type == 'multipart/form-data'):
                        application = self.upload(request, identifier)
            elif request.path_info.endswith('/upload/status'):
                identifier = request.GET.get(IDENTIFIER_KEY)

                if not self.manager.verify_identifier(identifier):
                    logger.error('Malformed upload identifier "%s"', identifier)
                    application = exc.HTTPServerError(
                        'Malformed upload identifier')
                else:
                    application = self.status(request, identifier)

        environ['infrae.fileupload.manager'] = self.manager
        return application(environ, start_response)

    def upload(self, request, identifier):
        """Handle upload of a file.
        """
        upload = None
        output_stream = None
        track_progress = None
        logger.debug('%s: New upload', identifier)

        try:
            length = request.content_length
            if self.max_size and int(length) > self.max_size:
                raise RequestError('Upload is too large')

            logger.debug('%s: Size checked', identifier)
            _, options = cgi.parse_header(request.headers['content-type'])
            if 'boundary' not in options:
                raise RequestError('Upload request is malformed '
                                   '(protocol error)')

            part_boundary = '--' + options['boundary']
            end_boundary = '--' + options['boundary'] + '--'

            logger.debug('%s: Content type boundary checked', identifier)
            input_stream = Reader(request.environ['wsgi.input'], length)
            # Read the first marker
            marker = input_stream.read(line=True)
            if marker.strip() != part_boundary:
                raise RequestError('Upload request is malformed '
                                   '(boundary error)')

            # Read the headers
            headers = {}
            line = input_stream.read(line=True)
            while not compare(line):
                name, payload = line.split(':', 1)
                headers[name.lower().strip()] = cgi.parse_header(payload)
                line = input_stream.read(line=True)

            # We should have now the payload
            if ('content-disposition' not in headers or
                'content-type' not in headers or
                headers['content-disposition'][0] != 'form-data' or
                not headers['content-disposition'][1].get('filename')):
                raise RequestError('Upload request is malformed '
                                   '(request is not a file upload)')
            logger.debug('%s: Upload header checked', identifier)

            upload = self.manager.create_upload_bucket(
                identifier,
                headers['content-disposition'][1]['filename'],
                headers['content-type'][0],
                length)
            logger.debug('%s: Upload started', identifier)

            track_progress = upload.progress()
            track_progress.send(None)
            input_stream.subscribe(track_progress.send)
            request.environ['infrae.fileupload.current'] = upload
            line = None
            output_stream = upload.write()
            while not compare(line, end_boundary):
                output_stream.send(line)
                line = input_stream.read()
                if line is None:
                    raise RequestError('Upload request is malformed '
                                       '(missing end of upload marker)')
                if compare(line, part_boundary):
                    # Multipart, we don't handle that
                    raise RequestError('Upload request is malformed '
                                       '(contains more than one request)')
        except UploadError as error:
            if upload is None:
                upload = self.manager.create_upload_bucket(
                    identifier, '', 'n/a', '0')
            upload.finish_upload(error=error.msg)
            return exc.HTTPServerError(error.msg)
        else:
            upload.finish_upload()
        finally:
            if output_stream is not None:
                output_stream.close()
            if track_progress is not None:
                track_progress.close()

        # We are done uploading
        logger.debug('%s: Upload done, file closed.', identifier)

        # Get the response from the application
        info = json.dumps(upload.get_status())
        request.environ['wsgi.input'] = io.BytesIO(info)
        request.content_type = 'application/json'
        request.content_length = len(info)
        return request.get_response(self.application)

    def clear(self, request, identifier):
        """Request an upload. This is called before starting the
        upload. To be sure the file is missing.
        """
        logger.debug('%s: Clear upload', identifier)
        try:
            upload = self.manager.access_upload_bucket(identifier)
            if upload is not None:
                upload.clear_upload()
        except UploadError:
            result = '{"success": false, "error": "Upload server error"}'
        else:
            result = '{"success": true}'
        response = Response()
        if 'callback' in request.GET:
            response.content_type = 'application/javascript'
            response.body = str(request.GET['callback']) + '(' + result + ')'
        else:
            response.content_type = 'application/json'
            response.body = result
        return response

    def status(self, request, identifier):
        """Handle status information on the upload process of a file.
        """
        logger.debug('%s: Status upload', identifier)
        result = {'state': 'starting'}
        try:
            upload = self.manager.access_upload_bucket(identifier)
            if upload is not None:
                status = upload.get_status()
                if status is not None:
                    result = status
        except UploadError as error:
            result = {'state': 'error', 'error': error.msg}

        response = Response()
        if 'callback' in request.GET:
            response.content_type = 'application/javascript'
            response.body = str(request.GET['callback']) + '(' + json.dumps(result) + ')'
        else:
            response.content_type = 'application/json'
            response.body = json.dumps(result)
        return response


def make_filter(application, global_conf, directory,
                max_size=0, upload_url=None, upload_key=None):
    """build a FileUpload application
    """
    directory = os.path.normpath(directory)
    if not os.path.isdir(directory):
        os.makedirs(directory)
    logger.info('Upload directory: %s' % directory)

    if max_size:
        # use Mo
        max_size = int(max_size)*1024*1024
        logger.info('Maximum upload size: %s' % max_size)

    if upload_url:
        if not upload_url.endswith('/upload'):
            upload_url += '/upload'
        logger.info('Uploading to external URL: %s' % upload_url)

    return UploadMiddleware(
        application, directory, max_size=max_size,
        upload_url=upload_url, upload_key=upload_key)
