=================
infrae.fileupload
=================

``infrae.fileupload`` is a WSGI component that can be used as a
middleware in order to intercept single file upload and save them on
the disk. The request to the application is then replaced by a json
body describing the information about the file. The original filename
and mimetype sent by the browser are kept as metadata. In addition to
this, it is possible to query the status of the upload and reset it
with HTTP request (by appending ``/status`` to the upload URL). An API is
exposed via the ``environ`` dictionnary provided the WSGI application
and can be used in order to query the files.

The API provides the following methods:

``create_identifier()``
    Create a new upload identifier that be provided while submitting
    the form using the ``X-Progress-ID`` parameter in the URL.

``verify_identifier(identifier)``
    Validate a given identifier.

``access_upload_bucket(identifier)``
    Access the given upload. An object with the following methods is
    returned:

    ``get_status()``
        Return a dictionnary containing metadata information about the upload.

    ``get_filename()``
        Return the filename of the uploaded file.

    ``is_complete()``
        Return True if the upload was complete.

    ``clear_upload()``
        Delete the upload.

``clear_upload_bucket(identifier)``
    Delete the given upload.

It is possible to use the component as a standalone WSGI application
and having a dedicated upload server available at a different URL.

Paster
======

The upload middleware (and upload application) can be used via Paster
using ``egg:infrae.fileupload``. Configuration directives are:

``directory``
    Pool directory where to store the uploaded files. If multiple
    processes accross multiple servers are configured for the same URL, they
    should share this directory too.

``max_size``
    Maximum allowed size for an upload in megabytes.

``upload_url``
    URL where to upload the files. If not specified the URL specified
    by the wsgi environment will be used. If it is specified, the
    middleware won't intercept uploads and assume this task is handled
    by the given URL. This is use in large installation in order to
    have a dedicated upload server by using the standalone application
    instead of the middleware. Upload URLs always ends with
    ``/upload``. If it is not the case it will appended to it.

``upload_key``
    Private key used to hash upload identifier. This enable a basic
    security preventing abuse of the upload middleware or
    application. If multiple processes and installations shares the
    same upload URL they should share the same upload key too.


Code repository
===============

You can find the code of this extension in Git:
https://github.com/infrae/infrae.fileupload
