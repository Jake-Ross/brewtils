# -*- coding: utf-8 -*-

import abc

import six


@six.add_metaclass(abc.ABCMeta)
class ResolverBase(object):
    """Base for all Resolver implementations"""

    def should_upload(self, value, definition):
        pass

    def should_download(self, value, definition):
        pass

    def upload(self, value, definition):
        pass

    def download(self, value, definition):
        pass
