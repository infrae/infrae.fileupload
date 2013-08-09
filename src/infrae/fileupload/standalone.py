
from webob import Response, exc
from infrae.fileupload.middleware import make_filter
import json


class StandaloneUpload(object):

    def __call__(self, environ, start_response):
        if 'infrae.fileupload.current' not in environ:
            application = exc.HTTPNotFound('Not uploading anything.')
        else:
            status = environ['infrae.fileupload.current'].get_status()
            application = Response()
            application.body = """
    <html>
        <body data-upload-identifier="%s" data-upload-info="%s">
        </body>
    </html>
        """ % (str(status['identifier']), json.dumps(status).replace('"', '\\"'))
        return application(environ, start_response)


def make_app(global_conf, **local_conf):
    """build a FileUpload application
    """
    if 'upload_url' in local_conf:
        del local_conf['upload_url']
    return make_filter(StandaloneUpload(), global_conf, **local_conf)

