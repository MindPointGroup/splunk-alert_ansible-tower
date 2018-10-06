# Copyright 2017, Ansible by Red Hat
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

import ast
import json
from distutils.util import strtobool

import click
import six

from tower_cli import models, resources, exceptions as exc
from tower_cli.api import client
from tower_cli.conf import pop_option
from tower_cli.cli import types
from tower_cli.utils.data_structures import OrderedDict


class Resource(models.Resource):
    """A resource for Tower configurations."""
    cli_help = 'Manage settings within Ansible Tower.'
    custom_category = None

    value = models.Field(required=True, type=types.Variables())

    @resources.command(ignore_defaults=True, no_args_is_help=False)
    @click.option('category', '-c', '--category',
                  help='If set, filter settings by a specific category')
    def list(self, **kwargs):
        """Return a list of objects.

        =====API DOCS=====
        Retrieve a list of Tower settings.

        :param category: The category slug in which to look up indevidual settings.
        :type category: str
        :param `**kwargs`: Keyword arguments list of available fields used for searching resource objects.
        :returns: A JSON object containing details of all resource objects returned by Tower backend.
        :rtype: dict

        =====API DOCS=====
        """
        self.custom_category = kwargs.get('category', 'all')
        try:
            result = super(Resource, self).list(**kwargs)
        except exc.NotFound as e:
            categories = map(
                lambda category: category['slug'],
                client.get('/settings/').json()['results']
            )
            e.message = '%s is not a valid category.  Choose from [%s]' % (
                kwargs['category'],
                ', '.join(categories)
            )
            raise e
        finally:
            self.custom_category = None
        return {
            'results': [{'id': k, 'value': v} for k, v in result.items()]
        }

    @resources.command(use_fields_as_options=False)
    def get(self, pk):
        """Return one and exactly one object

        =====API DOCS=====
        Return one and exactly one Tower setting.

        :param pk: Primary key of the Tower setting to retrieve
        :type pk: int
        :returns: loaded JSON of the retrieved Tower setting object.
        :rtype: dict
        :raises tower_cli.exceptions.NotFound: When no specified Tower setting exists.

        =====API DOCS=====
        """
        # The Tower API doesn't provide a mechanism for retrieving a single
        # setting value at a time, so fetch them all and filter
        try:
            return next(s for s in self.list()['results'] if s['id'] == pk)
        except StopIteration:
            raise exc.NotFound('The requested object could not be found.')

    @resources.command(use_fields_as_options=False)
    @click.argument('setting')
    @click.argument('value', default=None, required=False,
                    type=types.Variables())
    def modify(self, setting, value):
        """Modify an already existing object.

        Positional argument SETTING is the setting name and VALUE is its value,
        which can be provided directly or obtained from a file name if prefixed with '@'.

        =====API DOCS=====
        Modify an already existing Tower setting.

        :param setting: The name of the Tower setting to be modified.
        :type setting: str
        :param value: The new value of the Tower setting.
        :type value: str
        :returns: A dictionary combining the JSON output of the modified resource, as well as two extra fields:
                  "changed", a flag indicating if the resource is successfully updated; "id", an integer which
                  is the primary key of the updated object.
        :rtype: dict

        =====API DOCS=====
        """
        prev_value = new_value = self.get(setting)['value']
        answer = OrderedDict()
        encrypted = '$encrypted$' in six.text_type(prev_value)

        if encrypted or six.text_type(prev_value) != six.text_type(value):
            if setting == 'LICENSE':
                r = client.post('/config/',
                                data=self.coerce_type(setting, value))
                new_value = r.json()
            else:
                r = client.patch(
                    self.endpoint,
                    data={setting: self.coerce_type(setting, value)}
                )
                new_value = r.json()[setting]
            answer.update(r.json())

        changed = encrypted or (prev_value != new_value)

        answer.update({
            'changed': changed,
            'id': setting,
            'value': new_value,
        })
        return answer

    @property
    def endpoint(self):
        return '/settings/%s/' % (self.custom_category or 'all')

    def coerce_type(self, key, value):
        if key == 'LICENSE':
            return json.loads(value)
        r = client.options(self.endpoint)
        to_type = r.json()['actions']['PUT'].get(key, {}).get('type')
        if to_type == 'integer':
            return int(value)
        elif to_type == 'boolean':
            return bool(strtobool(value))
        elif to_type in ('list', 'nested object'):
            return ast.literal_eval(value)
        return value

    def __getattribute__(self, name):
        """Disable inherited methods that cannot be applied to this
        particular resource.
        """
        if name in ['create', 'delete']:
            raise AttributeError
        else:
            return object.__getattribute__(self, name)


# Settings don't support pagination, and there's nothing to filter on
pop_option(Resource.list, 'all_pages')
pop_option(Resource.list, 'page')
pop_option(Resource.list, 'query')

# Settings don't have a `create` operation
pop_option(Resource.modify, 'create_on_missing')
