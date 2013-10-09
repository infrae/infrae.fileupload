# -*- coding: utf-8 -*-
# Copyright (c) 2013 Infrae. All rights reserved.
# See also LICENSE.txt

from setuptools import setup, find_packages
import os

version = '1.0.1'

tests_require = [
    'infrae.wsgi [test]',
    ]


setup(name='infrae.fileupload',
      version=version,
      description="WSGI middleware to manage upload of files",
      long_description=open("README.txt").read() + "\n" +
                       open(os.path.join("docs", "HISTORY.txt")).read(),
      classifiers=[
        "License :: OSI Approved :: BSD License",
        "Programming Language :: Python",
        "Topic :: Software Development :: Libraries :: Python Modules",
        ],
      keywords='silva cms file upload zope',
      author='Infrae',
      author_email='info@infrae.com',
      url='http://infrae.com/products/silva',
      license='BSD',
      package_dir={'': 'src'},
      packages=find_packages('src'),
      namespace_packages=['infrae',],
      include_package_data=True,
      zip_safe=False,
      install_requires=[
        'setuptools',
        'WebOb'
        ],
      entry_points="""
      [paste.filter_app_factory]
      main = infrae.fileupload.middleware:make_filter
      [paste.app_factory]
      main = infrae.fileupload.standalone:make_app
      """,
      tests_require = tests_require,
      extras_require = {'test': tests_require},
      )
