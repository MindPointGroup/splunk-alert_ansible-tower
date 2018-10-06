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

from __future__ import absolute_import, unicode_literals

import click

from tower_cli import models, resources
from tower_cli.utils import parser
from tower_cli.api import client
from tower_cli.cli import types


class Resource(models.SurveyResource):
    """A resource for job templates."""
    cli_help = 'Manage job templates.'
    endpoint = '/job_templates/'
    dependencies = ['inventory', 'credential', 'project', 'vault_credential']
    related = ['survey_spec', 'notification_templates', 'extra_credentials', 'schedules']

    name = models.Field(unique=True)
    description = models.Field(required=False, display=False)
    job_type = models.Field(
        required=False,
        display=False,
        type=click.Choice(['run', 'check']),
    )
    inventory = models.Field(type=types.Related('inventory'), required=False)
    project = models.Field(type=types.Related('project'))
    playbook = models.Field()
    credential = models.Field(display=False, required=False, type=types.Related('credential'))
    vault_credential = models.Field(type=types.Related('credential'), required=False, display=False)
    forks = models.Field(type=int, required=False, display=False)
    limit = models.Field(required=False, display=False)
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
    extra_vars = models.Field(type=types.Variables(), required=False, display=False, multiple=True,
                              help_text='Extra variables used by Ansible in YAML or key=value '
                                        'format. Use @ to get YAML from a file.')
    job_tags = models.Field(required=False, display=False)
    force_handlers = models.Field(type=bool, required=False, display=False)
    skip_tags = models.Field(required=False, display=False)
    start_at_task = models.Field(required=False, display=False)
    timeout = models.Field(type=int, required=False, display=False,
                           help_text='The amount of time (in seconds) to run before the task is canceled.')
    use_fact_cache = models.Field(type=bool, required=False, display=False,
                                  help_text='If enabled, Tower will act as an Ansible Fact Cache Plugin;'
                                            ' persisting facts at the end of a playbook run to the database'
                                            ' and caching facts for use by Ansible.')
    host_config_key = models.Field(
        required=False, display=False,
        help_text='Allow Provisioning Callbacks using this host config key')
    ask_diff_mode_on_launch = models.Field(
        type=bool, required=False, display=False,
        help_text='Ask diff mode on launch.')
    ask_variables_on_launch = models.Field(
        type=bool, required=False, display=False,
        help_text='Prompt user for extra_vars on launch.')
    ask_limit_on_launch = models.Field(
        type=bool, required=False, display=False,
        help_text='Prompt user for host limits on launch.')
    ask_tags_on_launch = models.Field(
        type=bool, required=False, display=False,
        help_text='Prompt user for job tags on launch.')
    ask_skip_tags_on_launch = models.Field(
        type=bool, required=False, display=False,
        help_text='Prompt user for tags to skip on launch.')
    ask_job_type_on_launch = models.Field(
        type=bool, required=False, display=False,
        help_text='Prompt user for job type on launch.')
    ask_verbosity_on_launch = models.Field(
        type=bool, required=False, display=False,
        help_text='Prompt user for verbosity on launch.')
    ask_inventory_on_launch = models.Field(
        type=bool, required=False, display=False,
        help_text='Prompt user for inventory on launch.')
    ask_credential_on_launch = models.Field(
        type=bool, required=False, display=False,
        help_text='Prompt user for machine credential on launch.')
    survey_enabled = models.Field(
        type=bool, required=False, display=False,
        help_text='Prompt user for job type on launch.')
    become_enabled = models.Field(type=bool, required=False, display=False)
    diff_mode = models.Field(type=bool, required=False, display=False,
                             help_text='If enabled, textual changes made to any templated files on'
                                       ' the host are shown in the standard output.')
    allow_simultaneous = models.Field(type=bool, required=False, display=False)

    survey_spec = models.Field(
        type=types.Variables(), required=False, display=False,
        help_text='On write commands, perform extra POST to the '
                  'survey_spec endpoint.')

    labels = models.ManyToManyField('label')
    instance_groups = models.ManyToManyField('instance_group', method_name='ig')

    def write(self, *args, **kwargs):
        # Provide a default value for job_type, but only in creation of JT
        if (kwargs.get('create_on_missing', False) and
                (not kwargs.get('job_type', None))):
            kwargs['job_type'] = 'run'
        return super(Resource, self).write(*args, **kwargs)

    @resources.command(use_fields_as_options=False)
    @click.option('--job-template', type=types.Related('job_template'))
    @click.option('--credential', type=types.Related('credential'))
    def associate_credential(self, job_template, credential):
        """Associate a credential with this job template.

        =====API DOCS=====
        Associate a credential with this job template.

        :param job_template: The job template to associate to.
        :type job_template: str
        :param credential: The credential to be associated.
        :type credential: str
        :returns: Dictionary of only one key "changed", which indicates whether the association succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._assoc('extra_credentials', job_template, credential)

    @resources.command(use_fields_as_options=False)
    @click.option('--job-template', type=types.Related('job_template'))
    @click.option('--credential', type=types.Related('credential'))
    def disassociate_credential(self, job_template, credential):
        """Disassociate a credential with this job template.

        =====API DOCS=====
        Disassociate a credential from this job template.

        :param job_template: The job template to disassociate fom.
        :type job_template: str
        :param credential: The credential to be disassociated.
        :type credential: str
        :returns: Dictionary of only one key "changed", which indicates whether the disassociation succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._disassoc('extra_credentials', job_template, credential)

    @resources.command(use_fields_as_options=False)
    @click.option('--job-template', type=types.Related('job_template'))
    @click.option('--notification-template',
                  type=types.Related('notification_template'))
    @click.option('--status', type=click.Choice(['any', 'error', 'success']),
                  required=False, default='any', help='Specify job run status'
                  ' of job template to relate to.')
    def associate_notification_template(self, job_template,
                                        notification_template, status):
        """Associate a notification template from this job template.

        =====API DOCS=====
        Associate a notification template from this job template.

        :param job_template: The job template to associate to.
        :type job_template: str
        :param notification_template: The notification template to be associated.
        :type notification_template: str
        :param status: type of notification this notification template should be associated to.
        :type status: str
        :returns: Dictionary of only one key "changed", which indicates whether the association succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._assoc('notification_templates_%s' % status,
                           job_template, notification_template)

    @resources.command(use_fields_as_options=False)
    @click.option('--job-template', type=types.Related('job_template'))
    @click.option('--notification-template',
                  type=types.Related('notification_template'))
    @click.option('--status', type=click.Choice(['any', 'error', 'success']),
                  required=False, default='any', help='Specify job run status'
                  ' of job template to relate to.')
    def disassociate_notification_template(self, job_template,
                                           notification_template, status):
        """Disassociate a notification template from this job template.

        =====API DOCS=====
        Disassociate a notification template from this job template.

        :param job_template: The job template to disassociate from.
        :type job_template: str
        :param notification_template: The notification template to be disassociated.
        :type notification_template: str
        :param status: type of notification this notification template should be disassociated from.
        :type status: str
        :returns: Dictionary of only one key "changed", which indicates whether the disassociation succeeded.
        :rtype: dict

        =====API DOCS=====
        """
        return self._disassoc('notification_templates_%s' % status,
                              job_template, notification_template)

    @resources.command(use_fields_as_options=('extra_vars'))
    @click.option('--host-config-key', help='Job-template-specific string used to authenticate '
                  'host during provisioning callback.')
    def callback(self, pk=None, host_config_key='', extra_vars=None):
        """Contact Tower and request a configuration update using this job template.

        =====API DOCS=====
        Contact Tower and request a provisioning callback using this job template.

        :param pk: Primary key of the job template to run provisioning callback against.
        :type pk: int
        :param host_config_key: Key string used to authenticate the callback host.
        :type host_config_key: str
        :param extra_vars: Extra variables that are passed to provisioning callback.
        :type extra_vars: array of str
        :returns: A dictionary of a single key "changed", which indicates whether the provisioning callback
                  is successful.
        :rtype: dict

        =====API DOCS=====
        """
        url = self.endpoint + '%s/callback/' % pk
        if not host_config_key:
            host_config_key = client.get(url).json()['host_config_key']
        post_data = {'host_config_key': host_config_key}
        if extra_vars:
            post_data['extra_vars'] = parser.process_extra_vars(list(extra_vars), force_json=True)
        r = client.post(url, data=post_data, auth=None)
        if r.status_code == 201:
            return {'changed': True}
