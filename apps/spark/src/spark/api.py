#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import re
import time

from django.core.urlresolvers import reverse
from django.utils.html import escape
from django.utils.translation import ugettext as _

from spark.conf import SPARK_HOME, SPARK_MASTER
from desktop.lib.view_util import format_duration_in_millis
from filebrowser.views import location_to_url
from jobbrowser.views import job_single_logs
from liboozie.oozie_api import get_oozie
from oozie.models import Workflow, Shell
from oozie.views.editor import _submit_workflow

LOG = logging.getLogger(__name__)


def get(fs, jt, user):
  return OozieSparkApi(fs, jt, user)


class OozieSparkApi:
  """
  Oozie submission.
  """
  WORKFLOW_NAME = 'spark-app-hue-script'
  RE_LOG_END = re.compile('(<<< Invocation of Shell command completed <<<|<<< Invocation of Main class completed <<<)')
  RE_LOG_START_RUNNING = re.compile('>>> Invoking Shell command line now >>(.+?)(Exit code of the Shell|<<< Invocation of Shell command completed <<<|<<< Invocation of Main class completed)', re.M | re.DOTALL)
  RE_LOG_START_FINISHED = re.compile('(>>> Invoking Shell command line now >>)', re.M | re.DOTALL)
  MAX_DASHBOARD_JOBS = 100

  def __init__(self, fs, jt, user):
    self.fs = fs
    self.jt = jt
    self.user = user

  def submit(self, spark_script, params):
    mapping = {
      'oozie.use.system.libpath': 'false',
    }

    workflow = None

    try:
      workflow = self._create_workflow(spark_script, params)
      oozie_wf = _submit_workflow(self.user, self.fs, self.jt, workflow, mapping)
    finally:
      if workflow:
        workflow.delete()

    return oozie_wf

  def _create_workflow(self, spark_script, params):
    workflow = Workflow.objects.new_workflow(self.user)
    workflow.name = OozieSparkApi.WORKFLOW_NAME
    workflow.is_history = True
    workflow.save()
    Workflow.objects.initialize(workflow, self.fs)

    spark_script_path = workflow.deployment_dir + '/spark.script'
    spark_launcher_path = workflow.deployment_dir + '/spark.sh'
    self.fs.do_as_user(self.user.username, self.fs.create, spark_script_path, data=spark_script.dict['script'])
    self.fs.do_as_user(self.user.username, self.fs.create, spark_launcher_path, data="""
#!/usr/bin/env bash

WORKSPACE=`pwd`
cd %(spark_home)s
%(spark_master)s %(spark_shell)s $WORKSPACE/spark.script
    """ % {
        'spark_home': SPARK_HOME.get(),
        'spark_master': 'MASTER=%s' % SPARK_MASTER.get() if SPARK_MASTER.get() != '' else '',
        'spark_shell': 'pyspark' if spark_script.dict['language'] == 'python' else 'spark-shell <'
    })

    files = ['spark.script', 'spark.sh']
    archives = []

    popup_params = json.loads(params)
    popup_params_names = [param['name'] for param in popup_params]
    spark_params = self._build_parameters(popup_params)
    script_params = [param for param in spark_script.dict['parameters'] if param['name'] not in popup_params_names]

    spark_params += self._build_parameters(script_params)

    job_properties = [{"name": prop['name'], "value": prop['value']} for prop in spark_script.dict['hadoopProperties']]

    for resource in spark_script.dict['resources']:
      if resource['type'] == 'file':
        files.append(resource['value'])
      if resource['type'] == 'archive':
        archives.append({"dummy": "", "name": resource['value']})

    action = Shell.objects.create(
        name='spark',
        command='spark.sh',
        workflow=workflow,
        node_type='shell',
        params=json.dumps(spark_params),
        files=json.dumps(files),
        archives=json.dumps(archives),
        job_properties=json.dumps(job_properties),
    )

    action.add_node(workflow.end)

    start_link = workflow.start.get_link()
    start_link.child = action
    start_link.save()

    return workflow

  def _build_parameters(self, params):
    spark_params = []

    return spark_params

  def stop(self, job_id):
    return get_oozie(self.user).job_control(job_id, 'kill')

  def get_jobs(self):
    kwargs = {'cnt': OozieSparkApi.MAX_DASHBOARD_JOBS,}
    kwargs['user'] = self.user.username
    kwargs['name'] = OozieSparkApi.WORKFLOW_NAME

    return get_oozie(self.user).get_workflows(**kwargs).jobs

  def get_log(self, request, oozie_workflow):
    logs = {}

    for action in oozie_workflow.get_working_actions():
      try:
        if action.externalId:
          data = job_single_logs(request, **{'job': action.externalId})
          if data:
            matched_logs = self._match_logs(data)
            logs[action.name] = self._make_links(matched_logs)
      except Exception, e:
        LOG.error('An error happen while watching the demo running: %(error)s' % {'error': e})

    workflow_actions = []

    # Only one Shell action
    for action in oozie_workflow.get_working_actions():
      progress = get_progress(oozie_workflow, logs.get(action.name, ''))
      appendable = {
        'name': action.name,
        'status': action.status,
        'logs': logs.get(action.name, ''),
        'progress': progress,
        'progressPercent': '%d%%' % progress,
        'absoluteUrl': oozie_workflow.get_absolute_url(),
      }
      workflow_actions.append(appendable)

    return logs, workflow_actions

  def _match_logs(self, data):
    """Difficult to match multi lines of text"""
    logs = data['logs'][1]

    if OozieSparkApi.RE_LOG_END.search(logs):
      return re.search(OozieSparkApi.RE_LOG_START_RUNNING, logs).group(1).strip()
    else:
      group = re.search(OozieSparkApi.RE_LOG_START_FINISHED, logs)
      i = logs.index(group.group(1)) + len(group.group(1))
      return logs[i:].strip()

  @classmethod
  def _make_links(cls, log):
    escaped_logs = escape(log)
    hdfs_links = re.sub('((?<= |;)/|hdfs://)[^ <&\t;,\n]+', OozieSparkApi._make_hdfs_link, escaped_logs)
    return re.sub('(job_[0-9_]+(/|\.)?)', OozieSparkApi._make_mr_link, hdfs_links)

  @classmethod
  def _make_hdfs_link(self, match):
    try:
      return '<a href="%s" target="_blank">%s</a>' % (location_to_url(match.group(0), strict=False), match.group(0))
    except:
      return match.group(0)

  @classmethod
  def _make_mr_link(self, match):
    try:
      return '<a href="%s" target="_blank">%s</a>' % (reverse('jobbrowser.views.single_job', kwargs={'job': match.group(0)}), match.group(0))
    except:
      return match.group(0)

  def massaged_jobs_for_json(self, request, oozie_jobs, hue_jobs):
    jobs = []
    hue_jobs = dict([(script.dict.get('job_id'), script) for script in hue_jobs if script.dict.get('job_id')])

    for job in oozie_jobs:
      if job.is_running():
        job = get_oozie(self.user).get_job(job.id)
        get_copy = request.GET.copy() # Hacky, would need to refactor JobBrowser get logs
        get_copy['format'] = 'python'
        request.GET = get_copy
        try:
          logs, workflow_action = self.get_log(request, job)
          progress = workflow_action[0]['progress']
        except Exception:
          progress = 0
      else:
        progress = 100

      hue_pig = hue_jobs.get(job.id) and hue_jobs.get(job.id) or None

      massaged_job = {
        'id': job.id,
        'lastModTime': hasattr(job, 'lastModTime') and job.lastModTime and format_time(job.lastModTime) or None,
        'kickoffTime': hasattr(job, 'kickoffTime') and job.kickoffTime or None,
        'timeOut': hasattr(job, 'timeOut') and job.timeOut or None,
        'endTime': job.endTime and format_time(job.endTime) or None,
        'status': job.status,
        'isRunning': job.is_running(),
        'duration': job.endTime and job.startTime and format_duration_in_millis(( time.mktime(job.endTime) - time.mktime(job.startTime) ) * 1000) or None,
        'appName': hue_pig and hue_pig.dict['name'] or _('Unsaved script'),
        'scriptId': hue_pig and hue_pig.id or -1,
        'scriptContent': hue_pig and hue_pig.dict['script'] or '',
        'type': hue_pig and hue_pig.dict['type'] or '',
        'progress': progress,
        'progressPercent': '%d%%' % progress,
        'user': job.user,
        'absoluteUrl': job.get_absolute_url(),
        'canEdit': has_job_edition_permission(job, self.user),
        'killUrl': reverse('oozie:manage_oozie_jobs', kwargs={'job_id':job.id, 'action':'kill'}),
        'watchUrl': reverse('spark:watch', kwargs={'job_id': job.id}) + '?format=python',
        'created': hasattr(job, 'createdTime') and job.createdTime and job.createdTime and ((job.type == 'Bundle' and job.createdTime) or format_time(job.createdTime)),
        'startTime': hasattr(job, 'startTime') and format_time(job.startTime) or None,
        'run': hasattr(job, 'run') and job.run or 0,
        'frequency': hasattr(job, 'frequency') and job.frequency or None,
        'timeUnit': hasattr(job, 'timeUnit') and job.timeUnit or None,
        }
      jobs.append(massaged_job)

    return jobs

def get_progress(job, log):
  if job.status in ('SUCCEEDED', 'KILLED', 'FAILED'):
    return 100
  else:
    try:
      return int(re.findall("MapReduceLauncher  - (1?\d?\d)% complete", log)[-1])
    except:
      return 0


def format_time(st_time):
  if st_time is None:
    return '-'
  else:
    return time.strftime("%a, %d %b %Y %H:%M:%S", st_time)


def has_job_edition_permission(oozie_job, user):
  return user.is_superuser or oozie_job.user == user.username
