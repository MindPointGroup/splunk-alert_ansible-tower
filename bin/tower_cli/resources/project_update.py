# Copyright 2017, Red Hat, Inc.
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

from tower_cli import models
from tower_cli.constants import LAUNCH_TYPE_CHOICES, STATUS_CHOICES
from tower_cli.cli import types


class Resource(models.ExeResource):
    """A resource for project updates.
    """
    cli_help = 'Launch or monitor project updates.'
    endpoint = '/project_updates/'

    project = models.Field(
        key='-P',
        type=types.Related('project'), required=True, display=True
    )
    name = models.Field(required=False, display=True, read_only=True)
    launch_type = models.Field(
        type=click.Choice(LAUNCH_TYPE_CHOICES), read_only=True, display=False
    )
    status = models.Field(
        type=click.Choice(STATUS_CHOICES), read_only=True
    )
    job_type = models.Field(
        type=click.Choice(['run', 'check']), read_only=True
    )
    job_explanation = models.Field(required=False, display=False, read_only=True)
    created = models.Field(required=False, display=True, read_only=True)
    elapsed = models.Field(required=False, display=True, read_only=True)
    scm_type = models.Field(
        type=types.MappedChoice([
            ('', 'manual'),
            ('git', 'git'),
            ('hg', 'hg'),
            ('svn', 'svn'),
            ('insights', 'insights'),
        ]), display=False
    )
