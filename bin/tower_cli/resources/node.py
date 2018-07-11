# Copyright 2016, Ansible by Red Hat
# Alan Rominger <arominge@redhat.com>
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

from __future__ import absolute_import, unicode_literals

from tower_cli import models, resources, exceptions
from tower_cli.cli import types
from tower_cli.utils.resource_decorators import unified_job_template_options
from tower_cli.utils import debug
from tower_cli.api import client

import click


NODE_STANDARD_FIELDS = [
    'unified_job_template', 'inventory', 'credential', 'job_type',
    'job_tags', 'skip_tags', 'limit'
]
JOB_TYPES = {
    'job': 'job_template',
    'project_update': 'project',
    'inventory_update': 'inventory_source'
}


class Resource(models.Resource):
    """A resource for workflow nodes."""
    cli_help = 'Manage nodes inside of a workflow job template.'
    endpoint = '/workflow_job_template_nodes/'
    identity = ('id',)

    workflow_job_template = models.Field(
        key='-W', type=types.Related('workflow'))
    unified_job_template = models.Field(required=False)

    # Prompts
    extra_data = models.Field(type=types.Variables(), required=False,
                              display=False, help_text='Extra data for '
                              'schedule rules in the form of a .json file.')
    inventory = models.Field(
        type=types.Related('inventory'), required=False, display=False)
    credential = models.Field(
        type=types.Related('credential'), required=False, display=False)
    credentials = models.ManyToManyField('credential')
    job_type = models.Field(required=False, display=False)
    job_tags = models.Field(required=False, display=False)
    skip_tags = models.Field(required=False, display=False)
    limit = models.Field(required=False, display=False)
    diff_mode = models.Field(type=bool, required=False, display=False)
    verbosity = models.Field(
        display=False,
        type=types.MappedChoice([
            (0, 'default'),
            (1, 'verbose'),
            (2, 'more_verbose'),
            (3, 'debug'),
            (4, 'connection'),
            (5, 'winrm'),
        ]),
        required=False,
    )

    def __new__(cls, *args, **kwargs):
        for attr in ['create', 'modify', 'list']:
            setattr(cls, attr,
                    unified_job_template_options(getattr(cls, attr)))
        return super(Resource, cls).__new__(cls, *args, **kwargs)

    @staticmethod
    def _forward_rel_name(rel):
        return '{0}_nodes'.format(rel)

    @staticmethod
    def _reverse_rel_name(rel):
        return 'workflowjobtemplatenodes_{0}'.format(rel)

    def _parent_filter(self, parent, relationship, **kwargs):
        """
        Returns filtering parameters to limit a search to the children
        of a particular node by a particular relationship.
        """
        if parent is None or relationship is None:
            return {}
        parent_filter_kwargs = {}
        query_params = ((self._reverse_rel_name(relationship), parent),)
        parent_filter_kwargs['query'] = query_params
        if kwargs.get('workflow_job_template', None) is None:
            parent_data = self.read(pk=parent)['results'][0]
            parent_filter_kwargs['workflow_job_template'] = parent_data[
                'workflow_job_template']
        return parent_filter_kwargs

    @unified_job_template_options
    def _get_or_create_child(self, parent, relationship, **kwargs):
        ujt_pk = kwargs.get('unified_job_template', None)
        if ujt_pk is None:
            raise exceptions.BadRequest(
                'A child node must be specified by one of the options '
                'unified-job-template, job-template, project, or '
                'inventory-source')
        kwargs.update(self._parent_filter(parent, relationship, **kwargs))
        response = self.read(
            fail_on_no_results=False, fail_on_multiple_results=False, **kwargs)
        if len(response['results']) == 0:
            debug.log('Creating new workflow node.', header='details')
            return client.post(self.endpoint, data=kwargs).json()
        else:
            return response['results'][0]

    def _assoc_or_create(self, relationship, parent, child, **kwargs):
        if child is None:
            child_data = self._get_or_create_child(parent, relationship, **kwargs)
            return child_data
        return self._assoc(self._forward_rel_name(relationship), parent, child)

    @resources.command
    @unified_job_template_options
    @click.argument('parent', type=types.Related('node'))
    @click.argument('child', type=types.Related('node'), required=False)
    def associate_success_node(self, parent, child=None, **kwargs):
        """Add a node to run on success.

        =====API DOCS=====
        Add a node to run on success.

        :param parent: Primary key of parent node to associate success node to.
        :type parent: int
        :param child: Primary key of child node to be associated.
        :type child: int
        :param `**kwargs`: Fields used to create child node if ``child`` is not provided.
        :returns: Dictionary of only one key "changed", which indicates whether the association succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._assoc_or_create('success', parent, child, **kwargs)

    @resources.command(use_fields_as_options=False)
    @click.argument('parent', type=types.Related('node'))
    @click.argument('child', type=types.Related('node'))
    def disassociate_success_node(self, parent, child):
        """Remove success node.
        The resulatant 2 nodes will both become root nodes.

        =====API DOCS=====
        Remove success node.

        :param parent: Primary key of parent node to disassociate success node from.
        :type parent: int
        :param child: Primary key of child node to be disassociated.
        :type child: int
        :returns: Dictionary of only one key "changed", which indicates whether the disassociation succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._disassoc(
            self._forward_rel_name('success'), parent, child)

    @resources.command
    @unified_job_template_options
    @click.argument('parent', type=types.Related('node'))
    @click.argument('child', type=types.Related('node'), required=False)
    def associate_failure_node(self, parent, child=None, **kwargs):
        """Add a node to run on failure.

        =====API DOCS=====
        Add a node to run on failure.

        :param parent: Primary key of parent node to associate failure node to.
        :type parent: int
        :param child: Primary key of child node to be associated.
        :type child: int
        :param `**kwargs`: Fields used to create child node if ``child`` is not provided.
        :returns: Dictionary of only one key "changed", which indicates whether the association succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._assoc_or_create('failure', parent, child, **kwargs)

    @resources.command(use_fields_as_options=False)
    @click.argument('parent', type=types.Related('node'))
    @click.argument('child', type=types.Related('node'))
    def disassociate_failure_node(self, parent, child):
        """Remove a failure node link.
        The resulatant 2 nodes will both become root nodes.

        =====API DOCS=====
        Remove a failure node link.

        :param parent: Primary key of parent node to disassociate failure node from.
        :type parent: int
        :param child: Primary key of child node to be disassociated.
        :type child: int
        :returns: Dictionary of only one key "changed", which indicates whether the disassociation succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._disassoc(
            self._forward_rel_name('failure'), parent, child)

    @resources.command
    @unified_job_template_options
    @click.argument('parent', type=types.Related('node'))
    @click.argument('child', type=types.Related('node'), required=False)
    def associate_always_node(self, parent, child=None, **kwargs):
        """Add a node to always run after the parent is finished.

        =====API DOCS=====
        Add a node to always run after the parent is finished.

        :param parent: Primary key of parent node to associate always node to.
        :type parent: int
        :param child: Primary key of child node to be associated.
        :type child: int
        :param `**kwargs`: Fields used to create child node if ``child`` is not provided.
        :returns: Dictionary of only one key "changed", which indicates whether the association succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._assoc_or_create('always', parent, child, **kwargs)

    @resources.command(use_fields_as_options=False)
    @click.argument('parent', type=types.Related('node'))
    @click.argument('child', type=types.Related('node'))
    def disassociate_always_node(self, parent, child):
        """Remove an always node link.
        The resultant 2 nodes will both become root nodes.

        =====API DOCS=====
        Remove an always node link.

        :param parent: Primary key of parent node to disassociate always node from.
        :type parent: int
        :param child: Primary key of child node to be disassociated.
        :type child: int
        :returns: Dictionary of only one key "changed", which indicates whether the disassociation succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._disassoc(
            self._forward_rel_name('always'), parent, child)
