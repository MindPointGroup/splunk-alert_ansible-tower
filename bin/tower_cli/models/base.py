# Copyright 2015, Ansible, Inc.
# Luke Sneeringer <lsneeringer@ansible.com>
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import, division

import itertools
import json
import re
import sys
import time
from copy import copy
from base64 import b64decode

import six

import click
from click._compat import isatty as is_tty

from tower_cli import resources, exceptions as exc
from tower_cli.api import client
from tower_cli.conf import settings
from tower_cli.models.fields import Field, ManyToManyField
from tower_cli.utils import parser, debug, secho
from tower_cli.utils.data_structures import OrderedDict
from tower_cli.utils.resource_decorators import disabled_getter, disabled_setter, disabled_deleter


class ResourceMeta(type):
    """Metaclass for the creation of a Model subclass, which pulls fields
    aside into their appropriate tuple and handles other initialization.
    """
    def __new__(cls, name, bases, attrs):
        super_new = super(ResourceMeta, cls).__new__

        # ManyToManyField contributes extra methods, so those are
        # added here, before the method processing
        m2m_fields = []
        if not attrs.get('abstract', False):

            # Cowardly refuse to create a Resource with no endpoint (unless it's the base class).
            if not attrs.get('endpoint', None):
                raise TypeError('Resource subclass {} must have an `endpoint`.'.format(name))

            # Find all m2m fields
            for field_name, field in attrs.items():
                if isinstance(field, ManyToManyField):
                    field.configure_model(attrs, field_name)
                    m2m_fields.append(field)

            # Add associate / disassociate methods
            for field in m2m_fields:
                attrs[field.associate_method_name] = field.associate_method
                attrs[field.disassociate_method_name] = field.disassociate_method
                # tracked in m2m_fields, no longer need the field as direct attribute
                attrs.pop(field.relationship)

        # Mark all `@resources.command` methods as CLI commands.
        commands = set()
        for base in bases:
            base_commands = getattr(base, 'commands', [])
            commands = commands.union(base_commands)

        # Read list of deprecated resource methods if present.
        deprecates = attrs.pop('deprecated_methods', [])

        for key, value in attrs.items():
            if getattr(value, '_cli_command', False):
                commands.add(key)
                if key in deprecates:
                    setattr(value, 'deprecated', True)

            # If this method has been overwritten from the superclass, copy any click options or arguments from
            # the superclass implementation down to the subclass implementation.
            if not len(bases):
                continue
            cp = []
            baseattrs = {}
            for superclass in bases:
                super_method = getattr(superclass, key, None)
                if super_method and getattr(super_method, '_cli_command', False):
                    # Copy the click parameters from the parent method to the child.
                    for param in getattr(super_method, '__click_params__', []):
                        if param not in cp:
                            cp.append(param)

                    # Copy the command attributes from the parent to the child, if the child has not overridden them.
                    for attkey, attval in getattr(super_method, '_cli_command_attrs', {}).items():
                        baseattrs.setdefault(attkey, attval)
            if cp:
                # If subclass method is not decorated as command, but parent
                # classes do, then make it into a command here
                if not hasattr(value, '__click_params__'):
                    value.__click_params__ = []
                if not hasattr(value, '_cli_command_attrs'):
                    value._cli_command_attrs = {}

                # Copy all parent click parameters to subclass method here
                for param in cp:
                    if param not in value.__click_params__:
                        value.__click_params__.append(param)
                for attkey, attval in baseattrs.items():
                    value._cli_command_attrs.setdefault(attkey, attval)

        disabled_methods = attrs.pop('disabled_methods', set())
        commands -= disabled_methods
        attrs['commands'] = sorted(commands)
        for method in disabled_methods:
            attrs[method] = property(disabled_getter(method), disabled_setter(method), disabled_deleter(method))

        # Sanity check: Only perform remaining initialization for subclasses actual resources, not abstract ones.
        if attrs.pop('abstract', False):
            return super_new(cls, name, bases, attrs)

        # Initialize a new attributes dictionary.
        newattrs = {}

        # Iterate over each of the fields and move them into a `fields` list;
        # port remaining attrs unchanged into newattrs.
        fields = []
        unique_fields = set()
        for k, v in attrs.items():
            if isinstance(v, Field):
                v.name = k
                fields.append(v)
                if v.unique:
                    unique_fields.add(v.name)
            else:
                newattrs[k] = v
        newattrs['fields'] = sorted(fields)
        newattrs['m2m_fields'] = sorted(m2m_fields)
        newattrs['unique_fields'] = unique_fields

        # Ensure that the endpoint ends in a trailing slash, since we expect this when we build URLs based on it.
        if isinstance(newattrs['endpoint'], six.string_types):
            if not newattrs['endpoint'].startswith('/'):
                newattrs['endpoint'] = '/' + newattrs['endpoint']
            if not newattrs['endpoint'].endswith('/'):
                newattrs['endpoint'] += '/'

        # Construct the class.
        return super_new(cls, name, bases, newattrs)


class BaseResource(six.with_metaclass(ResourceMeta)):
    """Abstract class representing resources within the Ansible Tower system, on which actions can be taken.
    Includes standard create, modify, list, get, and delete methods.

    Some of these methods are not created as commands, but will be implemented as commands inside of non-abstract
    child classes. Particularly, create is not a command in this class, but will be for some (but not all) child
    classes."""
    abstract = True  # Not inherited.
    cli_help = ''
    endpoint = None
    identity = ('name',)
    dependencies = []
    related = []

    # The basic methods for interacting with a resource are `read`, `write`,
    # and `delete`; these cover basic CRUD situations and have options
    # to handle most desired behavior.
    #
    # Most likely, `read` and `write` won't see much direct use; rather,
    # `get` and `list` are wrappers around `read` and `create` and
    # `modify` are wrappers around `write`.

    def _pop_none(self, kwargs):
        """Remove default values (anything where the value is None). click is unfortunately bad at the way it
        sends through unspecified defaults."""
        for key, value in copy(kwargs).items():
            # options with multiple=True return a tuple
            if value is None or value == ():
                kwargs.pop(key)
            if hasattr(value, 'read'):
                kwargs[key] = value.read()

    def _lookup(self, fail_on_missing=False, fail_on_found=False, include_debug_header=True, **kwargs):
        """
        =====API DOCS=====
        Attempt to perform a lookup that is expected to return a single result, and return the record.

        This method is a wrapper around `get` that strips out non-unique keys, and is used internally by
        `write` and `delete`.

        :param fail_on_missing: Flag that raise exception if no resource is found.
        :type fail_on_missing: bool
        :param fail_on_found: Flag that raise exception if a resource is found.
        :type fail_on_found: bool
        :param include_debug_header: Flag determining whether to print debug messages when querying
                                     Tower backend.
        :type include_debug_header: bool
        :param `**kwargs`: Keyword arguments list of available fields used for searching resource.
        :returns: A JSON object containing details of the resource returned by Tower backend.
        :rtype: dict

        :raises tower_cli.exceptions.BadRequest: When no field are provided in kwargs.
        :raises tower_cli.exceptions.Found: When a resource is found and fail_on_found flag is on.
        :raises tower_cli.exceptions.NotFound: When no resource is found and fail_on_missing flag
                                               is on.
        =====API DOCS=====
        """
        read_params = {}
        for field_name in self.identity:
            if field_name in kwargs:
                read_params[field_name] = kwargs[field_name]
        if 'id' in self.identity and len(self.identity) == 1:
            return {}
        if not read_params:
            raise exc.BadRequest('Cannot reliably determine which record to write. Include an ID or unique '
                                 'fields.')
        try:
            existing_data = self.get(include_debug_header=include_debug_header, **read_params)
            if fail_on_found:
                raise exc.Found('A record matching %s already exists, and you requested a failure in that case.' %
                                read_params)
            return existing_data
        except exc.NotFound:
            if fail_on_missing:
                raise exc.NotFound('A record matching %s does not exist, and you requested a failure in that case.' %
                                   read_params)
            return {}

    def read(self, pk=None, fail_on_no_results=False, fail_on_multiple_results=False, **kwargs):
        """
        =====API DOCS=====
        Retrieve and return objects from the Ansible Tower API.

        :param pk: Primary key of the resource to be read. Tower CLI will only attempt to read that object
                   if ``pk`` is provided (not ``None``).
        :type pk: int
        :param fail_on_no_results: Flag that if set, zero results is considered a failure case and raises
                                   an exception; otherwise, empty list is returned. (Note: This is always True
                                   if a primary key is included.)
        :type fail_on_no_results: bool
        :param fail_on_multiple_results: Flag that if set, at most one result is expected, and more results
                                         constitutes a failure case. (Note: This is meaningless if a primary
                                         key is included, as there can never be multiple results.)
        :type fail_on_multiple_results: bool
        :param query: Contains 2-tuples used as query parameters to filter resulting resource objects.
        :type query: list
        :param `**kwargs`: Keyword arguments which, all together, will be used as query parameters to filter
                           resulting resource objects.
        :returns: loaded JSON from Tower backend response body.
        :rtype: dict
        :raises tower_cli.exceptions.BadRequest: When 2-tuples in ``query`` overlaps key-value pairs in
                                                 ``**kwargs``.
        :raises tower_cli.exceptions.NotFound: When no objects are found and ``fail_on_no_results`` flag is on.
        :raises tower_cli.exceptions.MultipleResults: When multiple objects are found and
                                                      ``fail_on_multiple_results`` flag is on.

        =====API DOCS=====
        """
        # Piece together the URL we will be hitting.
        url = self.endpoint
        if pk:
            url += '%s/' % pk

        # Pop the query parameter off of the keyword arguments; it will
        # require special handling (below).
        queries = kwargs.pop('query', [])

        # Remove default values (anything where the value is None).
        self._pop_none(kwargs)

        # Remove fields that are specifically excluded from lookup
        for field in self.fields:
            if field.no_lookup and field.name in kwargs:
                kwargs.pop(field.name)

        # If queries were provided, process them.
        for query in queries:
            if query[0] in kwargs:
                raise exc.BadRequest('Attempted to set %s twice.' % query[0].replace('_', '-'))
            kwargs[query[0]] = query[1]

        # Make the request to the Ansible Tower API.
        r = client.get(url, params=kwargs)
        resp = r.json()

        # If this was a request with a primary key included, then at the
        # point that we got a good result, we know that we're done and can
        # return the result.
        if pk:
            # Make the results all look the same, for easier parsing
            # by other methods.
            #
            # Note that the `get` method will effectively undo this operation,
            # but that's a good thing, because we might use `get` without a
            # primary key.
            return {'count': 1, 'results': [resp]}

        # Did we get zero results back when we shouldn't?
        # If so, this is an error, and we need to complain.
        if fail_on_no_results and resp['count'] == 0:
            raise exc.NotFound('The requested object could not be found.')

        # Did we get more than one result back?
        # If so, this is also an error, and we need to complain.
        if fail_on_multiple_results and resp['count'] >= 2:
            raise exc.MultipleResults('Expected one result, got %d. Possibly caused by not providing required '
                                      'fields. Please tighten your criteria.' % resp['count'])

        # Return the response.
        return resp

    def _get_patch_url(self, url, pk):
        """Overwrite this method to handle specific corner cases to the url passed to PATCH method."""
        return url + '%s/' % pk

    def write(self, pk=None, create_on_missing=False, fail_on_found=False, force_on_exists=True, **kwargs):
        """
        =====API DOCS=====
        Modify the given object using the Ansible Tower API.

        :param pk: Primary key of the resource to be read. Tower CLI will only attempt to read that object
                   if ``pk`` is provided (not ``None``).
        :type pk: int
        :param create_on_missing: Flag that if set, a new object is created if ``pk`` is not set and objects
                                  matching the appropriate unique criteria is not found.
        :type create_on_missing: bool
        :param fail_on_found: Flag that if set, the operation fails if an object matching the unique criteria
                              already exists.
        :type fail_on_found: bool
        :param force_on_exists: Flag that if set, then if an object is modified based on matching via unique
                                fields (as opposed to the primary key), other fields are updated based on data
                                sent; If unset, then the non-unique values are only written in a creation case.
        :type force_on_exists: bool
        :param `**kwargs`: Keyword arguments which, all together, will be used as POST/PATCH body to create/modify
                           the resource object. if ``pk`` is not set, key-value pairs of ``**kwargs`` which are
                           also in resource's identity will be used to lookup existing reosource.
        :returns: A dictionary combining the JSON output of the resource, as well as two extra fields: "changed",
                  a flag indicating if the resource is created or successfully updated; "id", an integer which
                  is the primary key of the specified object.
        :rtype: dict
        :raises tower_cli.exceptions.BadRequest: When required fields are missing in ``**kwargs`` when creating
                                                 a new resource object.

        =====API DOCS=====
        """
        existing_data = {}

        # Remove default values (anything where the value is None).
        self._pop_none(kwargs)

        # Determine which record we are writing, if we weren't given a primary key.
        if not pk:
            debug.log('Checking for an existing record.', header='details')
            existing_data = self._lookup(
                fail_on_found=fail_on_found, fail_on_missing=not create_on_missing, include_debug_header=False,
                **kwargs
            )
            if existing_data:
                pk = existing_data['id']
        else:
            # We already know the primary key, but get the existing data.
            # This allows us to know whether the write made any changes.
            debug.log('Getting existing record.', header='details')
            existing_data = self.get(pk)

        # Sanity check: Are we missing required values?
        # If we don't have a primary key, then all required values must be set, and if they're not, it's an error.
        missing_fields = []
        for i in self.fields:
            if i.key not in kwargs and i.name not in kwargs and i.required:
                missing_fields.append(i.key or i.name)
        if missing_fields and not pk:
            raise exc.BadRequest('Missing required fields: %s' % ', '.join(missing_fields).replace('_', '-'))

        # Sanity check: Do we need to do a write at all?
        # If `force_on_exists` is False and the record was, in fact, found, then no action is required.
        if pk and not force_on_exists:
            debug.log('Record already exists, and --force-on-exists is off; do nothing.', header='decision', nl=2)
            answer = OrderedDict((('changed', False), ('id', pk)))
            answer.update(existing_data)
            return answer

        # Similarly, if all existing data matches our write parameters, there's no need to do anything.
        if all([kwargs[k] == existing_data.get(k, None) for k in kwargs.keys()]):
            debug.log('All provided fields match existing data; do nothing.', header='decision', nl=2)
            answer = OrderedDict((('changed', False), ('id', pk)))
            answer.update(existing_data)
            return answer

        # Reinsert None for special case of null association
        for key in kwargs:
            if kwargs[key] == 'null':
                kwargs[key] = None

        # Get the URL and method to use for the write.
        url = self.endpoint
        method = 'POST'
        if pk:
            url = self._get_patch_url(url, pk)
            method = 'PATCH'

        # If debugging is on, print the URL and data being sent.
        debug.log('Writing the record.', header='details')

        # Actually perform the write.
        r = getattr(client, method.lower())(url, data=kwargs)

        # At this point, we know the write succeeded, and we know that data was changed in the process.
        answer = OrderedDict((('changed', True), ('id', r.json()['id'])))
        answer.update(r.json())
        return answer

    @resources.command
    def delete(self, pk=None, fail_on_missing=False, **kwargs):
        """Remove the given object.

        If `fail_on_missing` is True, then the object's not being found is considered a failure; otherwise,
        a success with no change is reported.

        =====API DOCS=====
        Remove the given object.

        :param pk: Primary key of the resource to be deleted.
        :type pk: int
        :param fail_on_missing: Flag that if set, the object's not being found is considered a failure; otherwise,
                                a success with no change is reported.
        :type fail_on_missing: bool
        :param `**kwargs`: Keyword arguments used to look up resource object to delete if ``pk`` is not provided.
        :returns: dictionary of only one field "changed", which is a flag indicating whether the specified resource
                  is successfully deleted.
        :rtype: dict

        =====API DOCS=====
        """
        # If we weren't given a primary key, determine which record we're deleting.
        if not pk:
            existing_data = self._lookup(fail_on_missing=fail_on_missing, **kwargs)
            if not existing_data:
                return {'changed': False}
            pk = existing_data['id']

        # Attempt to delete the record. If it turns out the record doesn't exist, handle the 404 appropriately
        # (this is an okay response if `fail_on_missing` is False).
        url = '%s%s/' % (self.endpoint, pk)
        debug.log('DELETE %s' % url, fg='blue', bold=True)
        try:
            client.delete(url)
            return {'changed': True}
        except exc.NotFound:
            if fail_on_missing:
                raise
            return {'changed': False}

    # Convenience wrappers around `read` and `write`:
    #   - read:  get, list
    #   - write: create, modify

    @resources.command(ignore_defaults=True)
    def get(self, pk=None, **kwargs):
        """Return one and exactly one object.

        Lookups may be through a primary key, specified as a positional argument, and/or through filters specified
        through keyword arguments.

        If the number of results does not equal one, raise an exception.

        =====API DOCS=====
        Retrieve one and exactly one object.

        :param pk: Primary key of the resource to be read. Tower CLI will only attempt to read *that* object
                   if ``pk`` is provided (not ``None``).
        :type pk: int
        :param `**kwargs`: Keyword arguments used to look up resource object to retrieve if ``pk`` is not provided.
        :returns: loaded JSON of the retrieved resource object.
        :rtype: dict

        =====API DOCS=====
        """
        if kwargs.pop('include_debug_header', True):
            debug.log('Getting the record.', header='details')
        response = self.read(pk=pk, fail_on_no_results=True, fail_on_multiple_results=True, **kwargs)
        return response['results'][0]

    @resources.command(ignore_defaults=True, no_args_is_help=False)
    @click.option('all_pages', '-a', '--all-pages', is_flag=True, default=False, show_default=True,
                  help='If set, collate all pages of content from the API when returning results.')
    @click.option('--page', default=1, type=int, show_default=True,
                  help='The page to show. Ignored if --all-pages is sent.')
    @click.option('--page-size', type=int, show_default=True, required=False,
                  help='Number of records to show. Ignored if --all-pages.')
    @click.option('-Q', '--query', required=False, nargs=2, multiple=True,
                  help='A key and value to be passed as an HTTP query string key and value to the Tower API.'
                       ' Will be run through HTTP escaping. This argument may be sent multiple times.\n'
                       'Example: `--query foo bar` would be passed to Tower as ?foo=bar')
    def list(self, all_pages=False, **kwargs):
        """Return a list of objects.

        If one or more filters are provided through keyword arguments, filter the results accordingly.

        If no filters are provided, return all results.

        =====API DOCS=====
        Retrieve a list of objects.

        :param all_pages: Flag that if set, collect all pages of content from the API when returning results.
        :type all_pages: bool
        :param page: The page to show. Ignored if all_pages is set.
        :type page: int
        :param query: Contains 2-tuples used as query parameters to filter resulting resource objects.
        :type query: list
        :param `**kwargs`: Keyword arguments list of available fields used for searching resource objects.
        :returns: A JSON object containing details of all resource objects returned by Tower backend.
        :rtype: dict

        =====API DOCS=====
        """
        # If the `all_pages` flag is set, then ignore any page that might also be sent.
        if all_pages:
            kwargs.pop('page', None)
            kwargs.pop('page_size', None)

        # Get the response.
        debug.log('Getting records.', header='details')
        response = self.read(**kwargs)

        # Alter the "next" and "previous" to reflect simple integers, rather than URLs, since this endpoint
        # just takes integers.
        for key in ('next', 'previous'):
            if not response.get(key):
                continue
            match = re.search(r'page=(?P<num>[\d]+)', response[key])
            if match is None and key == 'previous':
                response[key] = 1
                continue
            response[key] = int(match.groupdict()['num'])

        # If we were asked for all pages, keep retrieving pages until we have them all.
        if all_pages and response['next']:
            cursor = copy(response)
            while cursor['next']:
                cursor = self.list(**dict(kwargs, page=cursor['next']))
                response['results'] += cursor['results']

        # Done; return the response
        return response

    def _assoc(self, url_fragment, me, other):
        """Associate the `other` record with the `me` record."""

        # Get the endpoint for foreign records within this object.
        url = self.endpoint + '%d/%s/' % (me, url_fragment)

        # Attempt to determine whether the other record already exists here, for the "changed" moniker.
        r = client.get(url, params={'id': other}).json()
        if r['count'] > 0:
            return {'changed': False}

        # Send a request adding the other record to this one.
        r = client.post(url, data={'associate': True, 'id': other})
        return {'changed': True}

    def _disassoc(self, url_fragment, me, other):
        """Disassociate the `other` record from the `me` record."""

        # Get the endpoint for foreign records within this object.
        url = self.endpoint + '%d/%s/' % (me, url_fragment)

        # Attempt to determine whether the other record already is absent, for the "changed" moniker.
        r = client.get(url, params={'id': other}).json()
        if r['count'] == 0:
            return {'changed': False}

        # Send a request removing the foreign record from this one.
        r = client.post(url, data={'disassociate': True, 'id': other})
        return {'changed': True}


class Resource(BaseResource):
    """This is the parent class for all standard resources."""
    abstract = True

    @resources.command
    @click.option('--fail-on-found', default=False, show_default=True, type=bool, is_flag=True,
                  help='If used, return an error if a matching record already exists.')
    @click.option('--force-on-exists', default=False, show_default=True, type=bool, is_flag=True,
                  help='If used, if a match is found on unique fields, other fields will be updated '
                       'to the provided values. If False, a match causes the request to be a no-op.')
    def create(self, **kwargs):
        """Create an object.

        Fields in the resource's `identity` tuple are used for a lookup; if a match is found, then no-op
        (unless `force_on_exists` is set) but do not fail (unless `fail_on_found` is set).

        =====API DOCS=====
        Create an object.

        :param fail_on_found: Flag that if set, the operation fails if an object matching the unique criteria
                              already exists.
        :type fail_on_found: bool
        :param force_on_exists: Flag that if set, then if a match is found on unique fields, other fields will
                                be updated to the provided values.; If unset, a match causes the request to be
                                a no-op.
        :type force_on_exists: bool
        :param `**kwargs`: Keyword arguments which, all together, will be used as POST body to create the
                           resource object.
        :returns: A dictionary combining the JSON output of the created resource, as well as two extra fields:
                  "changed", a flag indicating if the resource is created successfully; "id", an integer which
                  is the primary key of the created object.
        :rtype: dict

        =====API DOCS=====
        """
        return self.write(create_on_missing=True, **kwargs)

    @resources.command(ignore_defaults=True)
    @click.option('--new-name', default=None,
                  help='The name to give the new resource, if used, will deep copy in the backend.')
    def copy(self, pk=None, new_name=None, **kwargs):
        """Copy an object.

        Only the ID is used for the lookup. All provided fields are used to override the old data from the
        copied resource.

        =====API DOCS=====
        Copy an object.

        :param pk: Primary key of the resource object to be copied
        :param new_name: The new name to give the resource if deep copying via the API
        :type pk: int
        :param `**kwargs`: Keyword arguments of fields whose given value will override the original value.
        :returns: loaded JSON of the copied new resource object.
        :rtype: dict

        =====API DOCS=====
        """
        orig = self.read(pk, fail_on_no_results=True, fail_on_multiple_results=True)
        orig = orig['results'][0]
        # Remove default values (anything where the value is None).
        self._pop_none(kwargs)

        newresource = copy(orig)
        newresource.pop('id')
        basename = newresource['name'].split('@', 1)[0].strip()

        # Modify data to fit the call pattern of the tower-cli method
        for field in self.fields:
            if field.multiple and field.name in newresource:
                newresource[field.name] = (newresource.get(field.name),)

        if new_name is None:
            # copy client-side, the old mechanism
            newresource.update(kwargs)
            newresource['name'] = "%s @ %s" % (basename, time.strftime('%X'))

            return self.write(create_on_missing=True, **newresource)
        else:
            # copy server-side, the new mechanism
            if kwargs:
                raise exc.TowerCLIError('Cannot override {} and also use --new-name.'.format(kwargs.keys()))
            copy_endpoint = '{}/{}/copy/'.format(self.endpoint.strip('/'), pk)
            return client.post(copy_endpoint, data={'name': new_name}).json()

    @resources.command(ignore_defaults=True)
    @click.option('--create-on-missing', default=False, show_default=True, type=bool, is_flag=True,
                  help='If used, and if options rather than a primary key are used to attempt to match a record, '
                       'will create the record if it does not exist. This is an alias to `create --force-on-exists`.')
    def modify(self, pk=None, create_on_missing=False, **kwargs):
        """Modify an already existing object.

        Fields in the resource's `identity` tuple can be used in lieu of a primary key for a lookup; in such a case,
        only other fields are written.

        To modify unique fields, you must use the primary key for the lookup.

        =====API DOCS=====
        Modify an already existing object.

        :param pk: Primary key of the resource to be modified.
        :type pk: int
        :param create_on_missing: Flag that if set, a new object is created if ``pk`` is not set and objects
                                  matching the appropriate unique criteria is not found.
        :type create_on_missing: bool
        :param `**kwargs`: Keyword arguments which, all together, will be used as PATCH body to modify the
                           resource object. if ``pk`` is not set, key-value pairs of ``**kwargs`` which are
                           also in resource's identity will be used to lookup existing reosource.
        :returns: A dictionary combining the JSON output of the modified resource, as well as two extra fields:
                  "changed", a flag indicating if the resource is successfully updated; "id", an integer which
                  is the primary key of the updated object.
        :rtype: dict

        =====API DOCS=====
        """
        return self.write(pk, create_on_missing=create_on_missing, force_on_exists=True, **kwargs)


class ReadOnlyResource(BaseResource):
    abstract = True
    disabled_methods = set(['_assoc', '_disassoc', '_get_patch_url', 'delete', 'write'])


class MonitorableResource(BaseResource):
    """A resource that is able to be tied to a running task, such as a job or project, and thus able to be monitored.
    """
    abstract = True  # Not inherited.

    def __init__(self, *args, **kwargs):
        if not hasattr(self, 'unified_job_type'):
            self.unified_job_type = self.endpoint
        return super(MonitorableResource, self).__init__(*args, **kwargs)

    def status(self, pk, detail=False):
        """A stub method requesting the status of the resource."""
        raise NotImplementedError('This resource does not implement a status method, and must do so.')

    def last_job_data(self, pk=None, **kwargs):
        """
        Internal utility function for Unified Job Templates. Returns data about the last job run off of that UJT
        """
        ujt = self.get(pk, include_debug_header=True, **kwargs)

        # Determine the appropriate inventory source update.
        if 'current_update' in ujt['related']:
            debug.log('A current job; retrieving it.', header='details')
            return client.get(ujt['related']['current_update'][7:]).json()
        elif ujt['related'].get('last_update', None):
            debug.log('No current job or update exists; retrieving the most recent.', header='details')
            return client.get(ujt['related']['last_update'][7:]).json()
        else:
            raise exc.NotFound('No related jobs or updates exist.')

    def lookup_stdout(self, pk=None, start_line=None, end_line=None, full=True):
        """
        Internal utility function to return standard out. Requires the pk of a unified job.
        """
        stdout_url = '%s%s/stdout/' % (self.unified_job_type, pk)
        payload = {'format': 'json', 'content_encoding': 'base64', 'content_format': 'ansi'}
        if start_line:
            payload['start_line'] = start_line
        if end_line:
            payload['end_line'] = end_line
        debug.log('Requesting a copy of job standard output', header='details')
        resp = client.get(stdout_url, params=payload).json()
        content = b64decode(resp['content'])

        return content

    @resources.command
    @click.option('--start-line', required=False, type=int, help='Line at which to start printing the standard out.')
    @click.option('--end-line', required=False, type=int, help='Line at which to end printing the standard out.')
    def stdout(self, pk, start_line=None, end_line=None, **kwargs):
        """
        Print out the standard out of a unified job to the command line.
        For Projects, print the standard out of most recent update.
        For Inventory Sources, print standard out of most recent sync.
        For Jobs, print the job's standard out.
        For Workflow Jobs, print a status table of its jobs.
        """
        # resource is Unified Job Template
        if self.unified_job_type != self.endpoint:
            unified_job = self.last_job_data(pk, **kwargs)
            pk = unified_job['id']
        # resource is Unified Job, but pk not given
        elif not pk:
            unified_job = self.get(**kwargs)
            pk = unified_job['id']

        content = self.lookup_stdout(pk, start_line, end_line)
        if len(content) > 0:
            click.echo(content, nl=1)

        return {"changed": False}

    @resources.command
    @click.option('--interval', default=0.2, help='Polling interval to refresh content from Tower.')
    @click.option('--timeout', required=False, type=int,
                  help='If provided, this command (not the job) will time out after the given number of seconds.')
    def monitor(self, pk, parent_pk=None, timeout=None, interval=0.5, outfile=sys.stdout, **kwargs):
        """
        Stream the standard output from a job, project update, or inventory udpate.

        =====API DOCS=====
        Stream the standard output from a job run to stdout.

        :param pk: Primary key of the job resource object to be monitored.
        :type pk: int
        :param parent_pk: Primary key of the unified job template resource object whose latest job run will be
                          monitored if ``pk`` is not set.
        :type parent_pk: int
        :param timeout: Number in seconds after which this method will time out.
        :type timeout: float
        :param interval: Polling interval to refresh content from Tower.
        :type interval: float
        :param outfile: Alternative file than stdout to write job stdout to.
        :type outfile: file
        :param `**kwargs`: Keyword arguments used to look up job resource object to monitor if ``pk`` is
                           not provided.
        :returns: A dictionary combining the JSON output of the finished job resource object, as well as
                  two extra fields: "changed", a flag indicating if the job resource object is finished
                  as expected; "id", an integer which is the primary key of the job resource object being
                  monitored.
        :rtype: dict
        :raises tower_cli.exceptions.Timeout: When monitor time reaches time out.
        :raises tower_cli.exceptions.JobFailure: When the job being monitored runs into failure.

        =====API DOCS=====
        """
        # If we do not have the unified job info, infer it from parent
        if pk is None:
            pk = self.last_job_data(parent_pk, **kwargs)['id']
        job_endpoint = '%s%s/' % (self.unified_job_type, pk)

        # Pause until job is in running state
        self.wait(pk, exit_on=['running', 'successful'])

        # Loop initialization
        start = time.time()
        start_line = 0
        result = client.get(job_endpoint).json()

        click.echo('\033[0;91m------Starting Standard Out Stream------\033[0m', nl=2, file=outfile)

        # Poll the Ansible Tower instance for status and content, and print standard out to the out file
        while not result['failed'] and result['status'] != 'successful':

            result = client.get(job_endpoint).json()

            # Put the process to sleep briefly.
            time.sleep(interval)

            # Make request to get standard out
            content = self.lookup_stdout(pk, start_line, full=False)

            # In the first moments of running the job, the standard out
            # may not be available yet
            if not six.text_type(content).startswith("Waiting for results"):
                line_count = len(content.splitlines())
                start_line += line_count
                click.echo(content, nl=0)

            if timeout and time.time() - start > timeout:
                raise exc.Timeout('Monitoring aborted due to timeout.')

        # Special final line for closure with workflow jobs
        if self.endpoint == '/workflow_jobs/':
            click.echo(self.lookup_stdout(pk, start_line, full=True), nl=1)

        click.echo('\033[0;91m------End of Standard Out Stream--------\033[0m', nl=2, file=outfile)

        if result['failed']:
            raise exc.JobFailure('Job failed.')

        # Return the job ID and other response data
        answer = OrderedDict((('changed', True), ('id', pk)))
        answer.update(result)
        # Make sure to return ID of resource and not update number relevant for project creation and update
        if parent_pk:
            answer['id'] = parent_pk
        else:
            answer['id'] = pk
        return answer

    @resources.command
    @click.option('--min-interval', default=1, help='The minimum interval to request an update from Tower.')
    @click.option('--max-interval', default=30, help='The maximum interval to request an update from Tower.')
    @click.option('--timeout', required=False, type=int,
                  help='If provided, this command (not the job) will time out after the given number of seconds.')
    def wait(self, pk, parent_pk=None, min_interval=1, max_interval=30, timeout=None, outfile=sys.stdout,
             exit_on=['successful'], **kwargs):
        """
        Wait for a running job to finish. Blocks further input until the job completes (whether successfully
        or unsuccessfully) and a final status can be given.

        =====API DOCS=====
        Wait for a job resource object to enter certain status.

        :param pk: Primary key of the job resource object to wait.
        :type pk: int
        :param parent_pk: Primary key of the unified job template resource object whose latest job run will be
                          waited if ``pk`` is not set.
        :type parent_pk: int
        :param timeout: Number in seconds after which this method will time out.
        :type timeout: float
        :param min_interval: Minimum polling interval to request an update from Tower.
        :type min_interval: float
        :param max_interval: Maximum polling interval to request an update from Tower.
        :type max_interval: float
        :param outfile: Alternative file than stdout to write job status updates on.
        :type outfile: file
        :param exit_on: Job resource object statuses to wait on.
        :type exit_on: array
        :param `**kwargs`: Keyword arguments used to look up job resource object to wait if ``pk`` is
                           not provided.
        :returns: A dictionary combining the JSON output of the status-changed job resource object, as well
                  as two extra fields: "changed", a flag indicating if the job resource object is status-changed
                  as expected; "id", an integer which is the primary key of the job resource object being
                  status-changed.
        :rtype: dict
        :raises tower_cli.exceptions.Timeout: When wait time reaches time out.
        :raises tower_cli.exceptions.JobFailure: When the job being waited on runs into failure.
        =====API DOCS=====
        """
        # If we do not have the unified job info, infer it from parent
        if pk is None:
            pk = self.last_job_data(parent_pk, **kwargs)['id']
        job_endpoint = '%s%s/' % (self.unified_job_type, pk)

        dots = itertools.cycle([0, 1, 2, 3])
        longest_string = 0
        interval = min_interval
        start = time.time()

        # Poll the Ansible Tower instance for status, and print the status to the outfile (usually standard out).
        #
        # Note that this is one of the few places where we use `secho` even though we're in a function that might
        # theoretically be imported and run in Python.  This seems fine; outfile can be set to /dev/null and very
        # much the normal use for this method should be CLI monitoring.
        result = client.get(job_endpoint).json()
        last_poll = time.time()
        timeout_check = 0
        while result['status'] not in exit_on:
            # If the job has failed, we want to raise an Exception for that so we get a non-zero response.
            if result['failed']:
                if is_tty(outfile) and not settings.verbose:
                    secho('\r' + ' ' * longest_string + '\n', file=outfile)
                raise exc.JobFailure('Job failed.')

            # Sanity check: Have we officially timed out?
            # The timeout check is incremented below, so this is checking to see if we were timed out as of
            # the previous iteration. If we are timed out, abort.
            if timeout and timeout_check - start > timeout:
                raise exc.Timeout('Monitoring aborted due to timeout.')

            # If the outfile is a TTY, print the current status.
            output = '\rCurrent status: %s%s' % (result['status'], '.' * next(dots))
            if longest_string > len(output):
                output += ' ' * (longest_string - len(output))
            else:
                longest_string = len(output)
            if is_tty(outfile) and not settings.verbose:
                secho(output, nl=False, file=outfile)

            # Put the process to sleep briefly.
            time.sleep(0.2)

            # Sanity check: Have we reached our timeout?
            # If we're about to time out, then we need to ensure that we do one last check.
            #
            # Note that the actual timeout will be performed at the start of the **next** iteration,
            # so there's a chance for the job's completion to be noted first.
            timeout_check = time.time()
            if timeout and timeout_check - start > timeout:
                last_poll -= interval

            # If enough time has elapsed, ask the server for a new status.
            #
            # Note that this doesn't actually do a status check every single time; we want the "spinner" to
            # spin even if we're not actively doing a check.
            #
            # So, what happens is that we are "counting down" (actually up) to the next time that we intend
            # to do a check, and once that time hits, we do the status check as part of the normal cycle.
            if time.time() - last_poll > interval:
                result = client.get(job_endpoint).json()
                last_poll = time.time()
                interval = min(interval * 1.5, max_interval)

                # If the outfile is *not* a TTY, print a status update when and only when we make an actual
                # check to job status.
                if not is_tty(outfile) or settings.verbose:
                    click.echo('Current status: %s' % result['status'], file=outfile)

            # Wipe out the previous output
            if is_tty(outfile) and not settings.verbose:
                secho('\r' + ' ' * longest_string, file=outfile, nl=False)
                secho('\r', file=outfile, nl=False)

        # Return the job ID and other response data
        answer = OrderedDict((('changed', True), ('id', pk)))
        answer.update(result)
        # Make sure to return ID of resource and not update number relevant for project creation and update
        if parent_pk:
            answer['id'] = parent_pk
        else:
            answer['id'] = pk
        return answer


class ExeResource(MonitorableResource):
    """Executable resource - defines status and cancel methods"""
    abstract = True

    @resources.command
    @click.option('--detail', is_flag=True, default=False, help='Print more detail.')
    def status(self, pk=None, detail=False, **kwargs):
        """Print the current job status. This is used to check a running job. You can look up the job with
        the same parameters used for a get request.

        =====API DOCS=====
        Retrieve the current job status.

        :param pk: Primary key of the resource to retrieve status from.
        :type pk: int
        :param detail: Flag that if set, return the full JSON of the job resource rather than a status summary.
        :type detail: bool
        :param `**kwargs`: Keyword arguments used to look up resource object to retrieve status from if ``pk``
                           is not provided.
        :returns: full loaded JSON of the specified unified job if ``detail`` flag is on; trimed JSON containing
                  only "elapsed", "failed" and "status" fields of the unified job if ``detail`` flag is off.
        :rtype: dict

        =====API DOCS=====
        """
        # Remove default values (anything where the value is None).
        self._pop_none(kwargs)

        # Search for the record if pk not given
        if not pk:
            job = self.get(include_debug_header=True, **kwargs)
        # Get the job from Ansible Tower if pk given
        else:
            debug.log('Asking for job status.', header='details')
            finished_endpoint = '%s%s/' % (self.endpoint, pk)
            job = client.get(finished_endpoint).json()

        # In most cases, we probably only want to know the status of the job and the amount of time elapsed.
        # However, if we were asked for verbose information, provide it.
        if detail:
            return job

        # Print just the information we need.
        return {
            'elapsed': job['elapsed'],
            'failed': job['failed'],
            'status': job['status'],
        }

    @resources.command
    @click.option('--fail-if-not-running', is_flag=True, default=False,
                  help='Fail loudly if the job is not currently running.')
    def cancel(self, pk=None, fail_if_not_running=False, **kwargs):
        """Cancel a currently running job.

        Fails with a non-zero exit status if the job cannot be canceled.
        You must provide either a pk or parameters in the job's identity.

        =====API DOCS=====
        Cancel a currently running job.

        :param pk: Primary key of the job resource to restart.
        :type pk: int
        :param fail_if_not_running: Flag that if set, raise exception if the job resource cannot be canceled.
        :type fail_if_not_running: bool
        :param `**kwargs`: Keyword arguments used to look up job resource object to restart if ``pk`` is not
                           provided.
        :returns: A dictionary of two keys: "status", which is "canceled", and "changed", which indicates if
                  the job resource has been successfully canceled.
        :rtype: dict
        :raises tower_cli.exceptions.TowerCLIError: When the job resource cannot be canceled and
                                                    ``fail_if_not_running`` flag is on.
        =====API DOCS=====
        """
        # Search for the record if pk not given
        if not pk:
            existing_data = self.get(**kwargs)
            pk = existing_data['id']

        cancel_endpoint = '%s%s/cancel/' % (self.endpoint, pk)
        # Attempt to cancel the job.
        try:
            client.post(cancel_endpoint)
            changed = True
        except exc.MethodNotAllowed:
            changed = False
            if fail_if_not_running:
                raise exc.TowerCLIError('Job not running.')

        # Return a success.
        return {'status': 'canceled', 'changed': changed}

    @resources.command
    def relaunch(self, pk=None, **kwargs):
        """Relaunch a stopped job.

        Fails with a non-zero exit status if the job cannot be relaunched.
        You must provide either a pk or parameters in the job's identity.

        =====API DOCS=====
        Relaunch a stopped job resource.

        :param pk: Primary key of the job resource to relaunch.
        :type pk: int
        :param `**kwargs`: Keyword arguments used to look up job resource object to relaunch if ``pk`` is not
                           provided.
        :returns: A dictionary combining the JSON output of the relaunched job resource object, as well
                  as an extra field "changed", a flag indicating if the job resource object is status-changed
                  as expected.
        :rtype: dict

        =====API DOCS=====
        """
        # Search for the record if pk not given
        if not pk:
            existing_data = self.get(**kwargs)
            pk = existing_data['id']

        relaunch_endpoint = '%s%s/relaunch/' % (self.endpoint, pk)
        data = {}
        # Attempt to relaunch the job.
        answer = {}
        try:
            result = client.post(relaunch_endpoint, data=data).json()
            if 'id' in result:
                answer.update(result)
            answer['changed'] = True
        except exc.MethodNotAllowed:
            answer['changed'] = False

        # Return the answer.
        return answer


class SurveyResource(Resource):
    """Contains utilities and commands common to "job template" models,
    which take extra_vars and have a survey_spec."""
    abstract = True

    def _survey_endpoint(self, pk):
        return '{0}{1}/survey_spec/'.format(self.endpoint, pk)

    def write(self, pk=None, **kwargs):
        survey_input = kwargs.pop('survey_spec', None)
        if kwargs.get('extra_vars', None):
            kwargs['extra_vars'] = parser.process_extra_vars(kwargs['extra_vars'])
        ret = super(SurveyResource, self).write(pk=pk, **kwargs)
        if survey_input is not None and ret.get('id', None):
            if not isinstance(survey_input, dict):
                survey_input = json.loads(survey_input.strip(' '))
            if survey_input == {}:
                debug.log('Deleting the survey_spec.', header='details')
                r = client.delete(self._survey_endpoint(ret['id']))
            else:
                debug.log('Saving the survey_spec.', header='details')
                r = client.post(self._survey_endpoint(ret['id']), data=survey_input)
            if r.status_code == 200:
                ret['changed'] = True
            if survey_input and not ret['survey_enabled']:
                debug.log('For survey to take effect, set survey_enabled field to True.', header='warning')
        return ret

    @resources.command
    def survey(self, pk=None, **kwargs):
        """Get the survey_spec for the job template.
        To write a survey, use the modify command with the --survey-spec parameter.

        =====API DOCS=====
        Get the survey specification of a resource object.

        :param pk: Primary key of the resource to retrieve survey from. Tower CLI will only attempt to
                   read *that* object if ``pk`` is provided (not ``None``).
        :type pk: int
        :param `**kwargs`: Keyword arguments used to look up resource object to retrieve survey if ``pk``
                           is not provided.
        :returns: loaded JSON of the retrieved survey specification of the resource object.
        :rtype: dict
        =====API DOCS=====
        """
        job_template = self.get(pk=pk, **kwargs)
        if settings.format == 'human':
            settings.format = 'json'
        return client.get(self._survey_endpoint(job_template['id'])).json()
