# -*- coding: utf-8 -*-
# Copyright 2009-2016 Yelp and Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for DataprocJobRunner"""
import copy
import getpass
import os
import os.path

from contextlib import contextmanager
from io import BytesIO

import mrjob
import mrjob.dataproc
from mrjob.dataproc import DataprocJobRunner
from mrjob.dataproc import _DEFAULT_IMAGE_VERSION

from mrjob.fs.gcs import GCSFilesystem
from mrjob.job import MRJob
from mrjob.parse import JOB_KEY_RE
from mrjob.parse import parse_gcs_uri
from mrjob.py2 import PY2
from mrjob.py2 import StringIO
from mrjob.step import StepFailedException
from mrjob.util import log_to_stream
from mrjob.util import tar_and_gzip

from tests.mockgoogleapiclient import MockGoogleAPITestCase

from tests.mr_hadoop_format_job import MRHadoopFormatJob
from tests.mr_no_mapper import MRNoMapper
from tests.mr_two_step_job import MRTwoStepJob
from tests.mr_word_count import MRWordCount
from tests.py2 import TestCase
from tests.py2 import mock
from tests.py2 import patch
from tests.quiet import logger_disabled
from tests.quiet import no_handlers_for_logger
from tests.sandbox import mrjob_conf_patcher
from tests.sandbox import patch_fs_gcs

try:
    from oauth2client.client import GoogleCredentials
    from googleapiclient import discovery
    from googleapiclient import errors as google_errors
except ImportError:
    # don't require googleapiclient; MRJobs don't actually need it when running
    # inside hadoop streaming
    GoogleCredentials = None
    discovery = None

# used to match command lines
if PY2:
    PYTHON_BIN = 'python'
else:
    PYTHON_BIN = 'python3'


class DataprocJobRunnerEndToEndTestCase(MockGoogleAPITestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'dataproc': {
        'cloud_api_cooldown_secs': 0.00,
        'fs_sync_secs': 0.00,
    }}}

    def test_end_to_end(self):
        # read from STDIN, a local file, and a remote file
        stdin = BytesIO(b'foo\nbar\n')

        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'wb') as local_input_file:
            local_input_file.write(b'bar\nqux\n')

        remote_input_path = 'gs://walrus/data/foo'
        self.put_gcs_data({'walrus': {'data/foo': b'foo\n'}})

        # setup fake output
        self.mock_dataproc_output = {('j-MOCKCLUSTER0', 1): [
            b'1\t"qux"\n2\t"bar"\n', b'2\t"foo"\n5\tnull\n']}

        mr_job = MRHadoopFormatJob(['-r', 'dataproc', '-v',
                                    '-', local_input_path, remote_input_path,
                                    '--jobconf', 'x=y'])
        mr_job.sandbox(stdin=stdin)

        local_tmp_dir = None
        results = []

        mock_gcs_fs_snapshot = copy.deepcopy(self.mock_gcs_fs)

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, DataprocJobRunner)

            # make sure that initializing the runner doesn't affect S3
            # (Issue #50)
            self.assertEqual(mock_gcs_fs_snapshot, self.mock_gcs_fs)

            # make sure AdditionalInfo was JSON-ified from the config file.
            # checked now because you can't actually read it from the cluster
            # on real Dataproc.
            self.assertEqual(runner._opts['additional_dataproc_info'],
                             '{"key": "value"}')
            runner.run()

            for line in runner.stream_output():
                key, value = mr_job.parse_output_line(line)
                results.append((key, value))

            local_tmp_dir = runner._get_local_tmp_dir()
            # make sure cleanup hasn't happened yet
            self.assertTrue(os.path.exists(local_tmp_dir))
            self.assertTrue(any(runner.fs.ls(runner.get_output_dir())))

            cluster = runner._describe_cluster()
            name_match = JOB_KEY_RE.match(cluster.name)
            self.assertEqual(name_match.group(1), 'mr_hadoop_format_job')
            self.assertEqual(name_match.group(2), getpass.getuser())

            # make sure our input and output formats are attached to
            # the correct steps
            dataproc_conn = runner.make_dataproc_conn()
            steps = list(_yield_all_steps(dataproc_conn, runner.get_cluster_id()))

            step_0_args = [a.value for a in steps[0].config.args]
            step_1_args = [a.value for a in steps[1].config.args]

            self.assertIn('-inputformat', step_0_args)
            self.assertNotIn('-outputformat', step_0_args)
            self.assertNotIn('-inputformat', step_1_args)
            self.assertIn('-outputformat', step_1_args)

            # make sure jobconf got through
            self.assertIn('-D', step_0_args)
            self.assertIn('x=y', step_0_args)
            self.assertIn('-D', step_1_args)
            # job overrides jobconf in step 1
            self.assertIn('x=z', step_1_args)

            # make sure mrjob.tar.gz is created and uploaded as
            # a bootstrap file
            self.assertTrue(os.path.exists(runner._mrjob_tar_gz_path))
            self.assertIn(runner._mrjob_tar_gz_path,
                          runner._upload_mgr.path_to_uri())
            self.assertIn(runner._mrjob_tar_gz_path,
                          runner._bootstrap_dir_mgr.paths())

        self.assertEqual(sorted(results),
                         [(1, 'qux'), (2, 'bar'), (2, 'foo'), (5, None)])

        # make sure cleanup happens
        self.assertFalse(os.path.exists(local_tmp_dir))
        self.assertFalse(any(runner.fs.ls(runner.get_output_dir())))

        # job should get terminated
        dataproc_conn = runner.make_dataproc_conn()
        cluster_id = runner.get_cluster_id()
        for _ in range(10):
            dataproc_conn.simulate_progress(cluster_id)

        cluster = runner._describe_cluster()
        self.assertEqual(cluster.status.state, 'TERMINATED')

    def test_failed_job(self):
        mr_job = MRTwoStepJob(['-r', 'dataproc', '-v'])
        mr_job.sandbox()

        self.put_gcs_data({'walrus': {}})
        self.mock_dataproc_failures = {('j-MOCKCLUSTER0', 0): None}

        with no_handlers_for_logger('mrjob.dataproc'):
            stderr = StringIO()
            log_to_stream('mrjob.dataproc', stderr)

            with mr_job.make_runner() as runner:
                self.assertIsInstance(runner, DataprocJobRunner)

                self.assertRaises(StepFailedException, runner.run)
                self.assertIn('\n  FAILED\n',
                              stderr.getvalue())

                dataproc_conn = runner.make_dataproc_conn()
                cluster_id = runner.get_cluster_id()
                for _ in range(10):
                    dataproc_conn.simulate_progress(cluster_id)

                cluster = runner._describe_cluster()
                self.assertEqual(cluster.status.state,
                                 'TERMINATED_WITH_ERRORS')

            # job should get terminated on cleanup
            cluster_id = runner.get_cluster_id()
            for _ in range(10):
                dataproc_conn.simulate_progress(cluster_id)

        cluster = runner._describe_cluster()
        self.assertEqual(cluster.status.state, 'TERMINATED_WITH_ERRORS')

    def _test_remote_tmp_cleanup(self, mode, tmp_len, log_len):
        self.put_gcs_data({'walrus': {'logs/j-MOCKCLUSTER0/1': b'1\n'}})
        stdin = BytesIO(b'foo\nbar\n')

        mr_job = MRTwoStepJob(['-r', 'dataproc', '-v',
                               '--s3-log-uri', 'gs://walrus/logs',
                               '-', '--cleanup', mode])
        mr_job.sandbox(stdin=stdin)

        with mr_job.make_runner() as runner:
            s3_tmp_dir = runner._opts['s3_tmp_dir']
            tmp_bucket, _ = parse_gcs_uri(s3_tmp_dir)

            runner.run()

            # this is set and unset before we can get at it unless we do this
            log_bucket, _ = parse_gcs_uri(runner._gcs_log_dir())

            list(runner.stream_output())

        conn = runner.fs.make_gcs_conn()
        bucket = conn.get_bucket(tmp_bucket)
        self.assertEqual(len(list(bucket.list())), tmp_len)

        bucket = conn.get_bucket(log_bucket)
        self.assertEqual(len(list(bucket.list())), log_len)

    def test_cleanup_all(self):
        self._test_remote_tmp_cleanup('ALL', 0, 0)

    def test_cleanup_tmp(self):
        self._test_remote_tmp_cleanup('TMP', 0, 1)

    def test_cleanup_remote(self):
        self._test_remote_tmp_cleanup('REMOTE_TMP', 0, 1)

    def test_cleanup_local(self):
        self._test_remote_tmp_cleanup('LOCAL_TMP', 5, 1)

    def test_cleanup_logs(self):
        self._test_remote_tmp_cleanup('LOGS', 5, 0)

    def test_cleanup_none(self):
        self._test_remote_tmp_cleanup('NONE', 5, 1)

    def test_cleanup_combine(self):
        self._test_remote_tmp_cleanup('LOGS,REMOTE_TMP', 0, 0)

    def test_cleanup_error(self):
        self.assertRaises(ValueError, self._test_remote_tmp_cleanup,
                          'NONE,LOGS,REMOTE_TMP', 0, 0)
        self.assertRaises(ValueError, self._test_remote_tmp_cleanup,
                          'GARBAGE', 0, 0)


class ExistingClusterTestCase(MockGoogleAPITestCase):

    def test_attach_to_existing_cluster(self):
        dataproc_conn = DataprocJobRunner(conf_paths=[]).make_dataproc_conn()
        # set log_uri to None, so that when we describe the cluster, it
        # won't have the loguri attribute, to test Issue #112
        cluster_id = dataproc_conn.run_jobflow(
            name='Development Cluster', log_uri=None,
            keep_alive=True, job_flow_role='fake-instance-profile',
            service_role='fake-service-role')

        stdin = BytesIO(b'foo\nbar\n')
        self.mock_dataproc_output = {(cluster_id, 1): [
            b'1\t"bar"\n1\t"foo"\n2\tnull\n']}

        mr_job = MRTwoStepJob(['-r', 'dataproc', '-v',
                               '--cluster-id', cluster_id])
        mr_job.sandbox(stdin=stdin)

        results = []
        with mr_job.make_runner() as runner:
            runner.run()

            # Issue 182: don't create the bootstrap script when
            # attaching to another cluster
            self.assertIsNone(runner._master_bootstrap_script_path)

            for line in runner.stream_output():
                key, value = mr_job.parse_output_line(line)
                results.append((key, value))

        self.assertEqual(sorted(results),
                         [(1, 'bar'), (1, 'foo'), (2, None)])

    def test_dont_take_down_cluster_on_failure(self):
        dataproc_conn = DataprocJobRunner(conf_paths=[]).make_dataproc_conn()
        # set log_uri to None, so that when we describe the cluster, it
        # won't have the loguri attribute, to test Issue #112
        cluster_id = dataproc_conn.run_jobflow(
            name='Development Cluster', log_uri=None,
            keep_alive=True, job_flow_role='fake-instance-profile',
            service_role='fake-service-role')

        mr_job = MRTwoStepJob(['-r', 'dataproc', '-v',
                               '--cluster-id', cluster_id])
        mr_job.sandbox()

        self.put_gcs_data({'walrus': {}})
        self.mock_dataproc_failures = set([('j-MOCKCLUSTER0', 0)])

        with mr_job.make_runner() as runner:
            self.assertIsInstance(runner, DataprocJobRunner)
            self.prepare_runner_for_ssh(runner)
            with logger_disabled('mrjob.dataproc'):
                self.assertRaises(StepFailedException, runner.run)

            dataproc_conn = runner.make_dataproc_conn()
            cluster_id = runner.get_cluster_id()
            for _ in range(10):
                dataproc_conn.simulate_progress(cluster_id)

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.status.state, 'WAITING')

        # job shouldn't get terminated by cleanup
        dataproc_conn = runner.make_dataproc_conn()
        cluster_id = runner.get_cluster_id()
        for _ in range(10):
            dataproc_conn.simulate_progress(cluster_id)

        cluster = runner._describe_cluster()
        self.assertEqual(cluster.status.state, 'WAITING')


class CloudAndHadoopVersionTestCase(MockGoogleAPITestCase):

    def test_default(self):
        with self.make_runner() as runner:
            runner.run()
            self.assertEqual(runner.get_cloud_version(), _DEFAULT_IMAGE_VERSION)
            self.assertEqual(runner.get_hadoop_version(), '2.7.2')

    def test_image_0_1(self):
        self._assert_cloud_hadoop_version('0.1', '2.7.1')

    def test_image_0_2(self):
        self._assert_cloud_hadoop_version('0.2', '2.7.1')

    def test_image_1_0(self):
        self._assert_cloud_hadoop_version('1.0', '2.7.2')

    def _assert_cloud_hadoop_version(self, cloud_version, hadoop_version):
        args = []
        with self.make_runner('--image-version', cloud_version) as runner:
            runner.run()
            self.assertEqual(runner.get_cloud_version(), cloud_version)
            self.assertEqual(runner.get_hadoop_version(), hadoop_version)

    def test_hadoop_version_option_does_nothing(self):
        with logger_disabled('mrjob.dataproc'):
            with self.make_runner('--hadoop-version', '1.2.3.4') as runner:
                runner.run()
                self.assertEqual(runner.get_cloud_version(),
                                 _DEFAULT_IMAGE_VERSION)
                self.assertEqual(runner.get_hadoop_version(), '2.7.2')


class ZoneTestCase(MockGoogleAPITestCase):

    MRJOB_CONF_CONTENTS = {'runners': {'dataproc': {
        'cloud_api_cooldown_secs': 0.00,
        'fs_sync_secs': 0.00,
        'aws_availability_zone': 'PUPPYLAND',
    }}}

    def test_availability_zone_config(self):
        with self.make_runner() as runner:
            runner.run()

            cluster = runner._describe_cluster()
            self.assertEqual(cluster.ec2instanceattributes.ec2availabilityzone,
                             'PUPPYLAND')


class RegionTestCase(MockGoogleAPITestCase):

    def test_default(self):
        runner = DataprocJobRunner()
        self.assertEqual(runner._opts['region'], 'us-west-2')

    def test_explicit_region(self):
        runner = DataprocJobRunner(region='us-east-1')
        self.assertEqual(runner._opts['region'], 'us-east-1')

    def test_cannot_be_empty(self):
        runner = DataprocJobRunner(region='')
        self.assertEqual(runner._opts['region'], 'us-west-2')


class TmpBucketTestCase(MockGoogleAPITestCase):

    def assert_new_tmp_bucket(self, location, **runner_kwargs):
        """Assert that if we create an DataprocJobRunner with the given keyword
        args, it'll create a new tmp bucket with the given location
        constraint.
        """
        existing_bucket_names = set(self.mock_gcs_fs)

        runner = DataprocJobRunner(conf_paths=[], **runner_kwargs)
        runner._create_gcs_tmp_bucket_if_needed()

        bucket_name, path = parse_gcs_uri(runner._opts['s3_tmp_dir'])

        self.assertTrue(bucket_name.startswith('mrjob-'))
        self.assertNotIn(bucket_name, existing_bucket_names)
        self.assertEqual(path, 'tmp/')

        self.assertEqual(self.mock_gcs_fs[bucket_name]['location'], location)

    def test_default(self):
        self.assert_new_tmp_bucket('us-west-2')

    def test_us_west_1(self):
        self.assert_new_tmp_bucket('us-west-1',
                                       region='us-west-1')

    def test_us_east_1(self):
        # location should be blank
        self.assert_new_tmp_bucket('',
                                       region='us-east-1')

    def test_reuse_mrjob_bucket_in_same_region(self):
        self.put_gcs_data({'mrjob-1': {}}, location='us-west-2')

        runner = DataprocJobRunner()
        self.assertEqual(runner._opts['s3_tmp_dir'],
                         'gs://mrjob-1/tmp/')

    def test_ignore_mrjob_bucket_in_different_region(self):
        # this tests 687
        self.put_gcs_data({'mrjob-1': {}}, location='')

        self.assert_new_tmp_bucket('us-west-2')

    def test_ignore_non_mrjob_bucket_in_different_region(self):
        self.put_gcs_data({'walrus': {}}, location='us-west-2')

        self.assert_new_tmp_bucket('us-west-2')

    def test_reuse_mrjob_bucket_in_us_east_1(self):
        # us-east-1 is special because the location "constraint" for its
        # buckets is '', not 'us-east-1'
        self.put_gcs_data({'mrjob-1': {}}, location='')

        runner = DataprocJobRunner(region='us-east-1')

        self.assertEqual(runner._opts['s3_tmp_dir'],
                         'gs://mrjob-1/tmp/')

    def test_explicit_tmp_uri(self):
        self.put_gcs_data({'walrus': {}}, location='us-west-2')

        runner = DataprocJobRunner(s3_tmp_dir='gs://walrus/tmp/')

        self.assertEqual(runner._opts['s3_tmp_dir'],
                         'gs://walrus/tmp/')

    def test_cross_region_explicit_tmp_uri(self):
        self.put_gcs_data({'walrus': {}}, location='us-west-2')

        runner = DataprocJobRunner(region='us-west-1',
                              s3_tmp_dir='gs://walrus/tmp/')

        self.assertEqual(runner._opts['s3_tmp_dir'],
                         'gs://walrus/tmp/')

        # tmp bucket shouldn't influence region (it did in 0.4.x)
        self.assertEqual(runner._opts['region'], 'us-west-1')


class EC2InstanceGroupTestCase(MockGoogleAPITestCase):

    maxDiff = None

    def _test_instance_groups(self, opts, **expected):
        """Run a job with the given option dictionary, and check for
        for instance, number, and optional bid price for each instance role.

        Specify expected instance group info like:

        <role>=(num_instances, instance_type, bid_price)
        """
        runner = DataprocJobRunner(**opts)
        cluster_id = runner.make_persistent_cluster()

        dataproc_conn = runner.make_dataproc_conn()
        instance_groups = list(
            _yield_all_instance_groups(dataproc_conn, cluster_id))

        # convert actual instance groups to dicts
        role_to_actual = {}
        for ig in instance_groups:
            info = dict(
                (field, getattr(ig, field, None))
                for field in ('bidprice', 'instancetype',
                              'market', 'requestedinstancecount'))

            role_to_actual[ig.instancegrouptype] = info

        # convert expected to dicts
        role_to_expected = {}
        for role, (num, instance_type, bid_price) in expected.items():
            info = dict(
                bidprice=(bid_price if bid_price else None),
                instancetype=instance_type,
                market=(u'SPOT' if bid_price else u'ON_DEMAND'),
                requestedinstancecount=str(num),
            )

            role_to_expected[role.upper()] = info

        self.assertEqual(role_to_actual, role_to_expected)

    def set_in_mrjob_conf(self, **kwargs):
        dataproc_opts = copy.deepcopy(self.MRJOB_CONF_CONTENTS)
        dataproc_opts['runners']['dataproc'].update(kwargs)
        patcher = mrjob_conf_patcher(dataproc_opts)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_defaults(self):
        self._test_instance_groups(
            {},
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'num_ec2_instances': 3},
            core=(2, 'm1.medium', None),
            master=(1, 'm1.medium', None))

    def test_single_instance(self):
        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge'},
            master=(1, 'c1.xlarge', None))

    def test_multiple_instances(self):
        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge', 'num_ec2_instances': 3},
            core=(2, 'c1.xlarge', None),
            master=(1, 'm1.medium', None))

    def test_explicit_master_and_slave_instance_types(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large'},
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_slave_instance_type': 'm2.xlarge',
             'num_ec2_instances': 3},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'm2.xlarge',
             'num_ec2_instances': 3},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

    def test_explicit_instance_types_take_precedence(self):
        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge',
             'ec2_master_instance_type': 'm1.large'},
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge',
             'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'm2.xlarge',
             'num_ec2_instances': 3},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

    def test_cmd_line_opts_beat_mrjob_conf(self):
        # set ec2_instance_type in mrjob.conf, 1 instance
        self.set_in_mrjob_conf(ec2_instance_type='c1.xlarge')

        self._test_instance_groups(
            {},
            master=(1, 'c1.xlarge', None))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large'},
            master=(1, 'm1.large', None))

        # set ec2_instance_type in mrjob.conf, 3 instances
        self.set_in_mrjob_conf(ec2_instance_type='c1.xlarge',
                               num_ec2_instances=3)

        self._test_instance_groups(
            {},
            core=(2, 'c1.xlarge', None),
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'm2.xlarge'},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

        # set master in mrjob.conf, 1 instance
        self.set_in_mrjob_conf(ec2_master_instance_type='m1.large')

        self._test_instance_groups(
            {},
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge'},
            master=(1, 'c1.xlarge', None))

        # set master and slave in mrjob.conf, 2 instances
        self.set_in_mrjob_conf(ec2_master_instance_type='m1.large',
                               ec2_slave_instance_type='m2.xlarge',
                               num_ec2_instances=3)

        self._test_instance_groups(
            {},
            core=(2, 'm2.xlarge', None),
            master=(1, 'm1.large', None))

        self._test_instance_groups(
            {'ec2_instance_type': 'c1.xlarge'},
            core=(2, 'c1.xlarge', None),
            master=(1, 'm1.large', None))

    def test_zero_core_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'c1.medium',
             'num_ec2_core_instances': 0},
            master=(1, 'c1.medium', None))

    def test_core_spot_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'ec2_core_instance_bid_price': '0.20',
             'num_ec2_core_instances': 5},
            core=(5, 'c1.medium', '0.20'),
            master=(1, 'm1.large', None))

    def test_core_on_demand_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 5},
            core=(5, 'c1.medium', None),
            master=(1, 'm1.large', None))

        # Test the ec2_slave_instance_type alias
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_slave_instance_type': 'c1.medium',
             'num_ec2_instances': 6},
            core=(5, 'c1.medium', None),
            master=(1, 'm1.large', None))

    def test_core_and_task_on_demand_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 5,
             'ec2_task_instance_type': 'm2.xlarge',
             'num_ec2_task_instances': 20,
             },
            core=(5, 'c1.medium', None),
            master=(1, 'm1.large', None),
            task=(20, 'm2.xlarge', None))

    def test_core_and_task_spot_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'ec2_core_instance_bid_price': '0.20',
             'num_ec2_core_instances': 10,
             'ec2_task_instance_type': 'm2.xlarge',
             'ec2_task_instance_bid_price': '1.00',
             'num_ec2_task_instances': 20,
             },
            core=(10, 'c1.medium', '0.20'),
            master=(1, 'm1.large', None),
            task=(20, 'm2.xlarge', '1.00'))

        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 10,
             'ec2_task_instance_type': 'm2.xlarge',
             'ec2_task_instance_bid_price': '1.00',
             'num_ec2_task_instances': 20,
             },
            core=(10, 'c1.medium', None),
            master=(1, 'm1.large', None),
            task=(20, 'm2.xlarge', '1.00'))

    def test_master_and_core_spot_instances(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_master_instance_bid_price': '0.50',
             'ec2_core_instance_type': 'c1.medium',
             'ec2_core_instance_bid_price': '0.20',
             'num_ec2_core_instances': 10,
             },
            core=(10, 'c1.medium', '0.20'),
            master=(1, 'm1.large', '0.50'))

    def test_master_spot_instance(self):
        self._test_instance_groups(
            {'ec2_master_instance_type': 'm1.large',
             'ec2_master_instance_bid_price': '0.50',
             },
            master=(1, 'm1.large', '0.50'))

    def test_zero_or_blank_bid_price_means_on_demand(self):
        self._test_instance_groups(
            {'ec2_master_instance_bid_price': '0',
             },
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'num_ec2_core_instances': 3,
             'ec2_core_instance_bid_price': '0.00',
             },
            core=(3, 'm1.medium', None),
            master=(1, 'm1.medium', None))

        self._test_instance_groups(
            {'num_ec2_core_instances': 3,
             'num_ec2_task_instances': 5,
             'ec2_task_instance_bid_price': '',
             },
            core=(3, 'm1.medium', None),
            master=(1, 'm1.medium', None),
            task=(5, 'm1.medium', None))

    def test_pass_invalid_bid_prices_through_to_dataproc(self):
        self.assertRaises(
            boto.exception.EmrResponseError,
            self._test_instance_groups,
            {'ec2_master_instance_bid_price': 'all the gold in California'})

    def test_task_type_defaults_to_core_type(self):
        self._test_instance_groups(
            {'ec2_core_instance_type': 'c1.medium',
             'num_ec2_core_instances': 5,
             'num_ec2_task_instances': 20,
             },
            core=(5, 'c1.medium', None),
            master=(1, 'm1.medium', None),
            task=(20, 'c1.medium', None))

    def test_mixing_instance_number_opts_on_cmd_line(self):
        stderr = StringIO()
        with no_handlers_for_logger():
            log_to_stream('mrjob.dataproc', stderr)
            self._test_instance_groups(
                {'num_ec2_instances': 4,
                 'num_ec2_core_instances': 10},
                core=(10, 'm1.medium', None),
                master=(1, 'm1.medium', None))

        self.assertIn('does not make sense', stderr.getvalue())

    def test_mixing_instance_number_opts_in_mrjob_conf(self):
        self.set_in_mrjob_conf(num_ec2_instances=3,
                               num_ec2_core_instances=5,
                               num_ec2_task_instances=9)

        stderr = StringIO()
        with no_handlers_for_logger():
            log_to_stream('mrjob.dataproc', stderr)
            self._test_instance_groups(
                {},
                core=(5, 'm1.medium', None),
                master=(1, 'm1.medium', None),
                task=(9, 'm1.medium', None))

        self.assertIn('does not make sense', stderr.getvalue())

    def test_cmd_line_instance_numbers_beat_mrjob_conf(self):
        self.set_in_mrjob_conf(num_ec2_core_instances=5,
                               num_ec2_task_instances=9)

        stderr = StringIO()
        with no_handlers_for_logger():
            log_to_stream('mrjob.dataproc', stderr)
            self._test_instance_groups(
                {'num_ec2_instances': 3},
                core=(2, 'm1.medium', None),
                master=(1, 'm1.medium', None))

        self.assertNotIn('does not make sense', stderr.getvalue())


class TestNoBoto(TestCase):

    def setUp(self):
        self.blank_out_boto()

    def tearDown(self):
        self.restore_boto()

    def blank_out_boto(self):
        self._real_boto = mrjob.dataproc.boto
        mrjob.dataproc.boto = None
        mrjob.fs.s3.boto = None

    def restore_boto(self):
        mrjob.dataproc.boto = self._real_boto
        mrjob.fs.s3.boto = self._real_boto

    def test_init(self):
        # merely creating an DataprocJobRunner should raise an exception
        # because it'll need to connect to S3 to set s3_tmp_dir
        self.assertRaises(ImportError, DataprocJobRunner, conf_paths=[])


class TestMasterBootstrapScript(MockGoogleAPITestCase):

    def test_usr_bin_env(self):
        runner = DataprocJobRunner(conf_paths=[],
                              bootstrap_mrjob=True,
                              sh_bin='bash -e')

        runner._add_bootstrap_files_for_upload()

        self.assertIsNotNone(runner._master_bootstrap_script_path)
        self.assertTrue(os.path.exists(runner._master_bootstrap_script_path))

        with open(runner._master_bootstrap_script_path) as f:
            lines = [line.rstrip() for line in f]

        self.assertEqual(lines[0], '#!/usr/bin/env bash -e')

    def test_create_master_bootstrap_script(self):
        # create a fake src tarball
        foo_py_path = os.path.join(self.tmp_dir, 'foo.py')
        with open(foo_py_path, 'w'):
            pass

        yelpy_tar_gz_path = os.path.join(self.tmp_dir, 'yelpy.tar.gz')
        tar_and_gzip(self.tmp_dir, yelpy_tar_gz_path, prefix='yelpy')

        # use all the bootstrap options
        runner = DataprocJobRunner(conf_paths=[],
                              bootstrap=[
                                  PYTHON_BIN + ' ' +
                                  foo_py_path + '#bar.py',
                                  'gs://walrus/scripts/ohnoes.sh#'],
                              bootstrap_cmds=['echo "Hi!"', 'true', 'ls'],
                              bootstrap_files=['/tmp/quz'],
                              bootstrap_mrjob=True,
                              bootstrap_python_packages=[yelpy_tar_gz_path],
                              bootstrap_scripts=['speedups.sh', '/tmp/s.sh'])

        runner._add_bootstrap_files_for_upload()

        self.assertIsNotNone(runner._master_bootstrap_script_path)
        self.assertTrue(os.path.exists(runner._master_bootstrap_script_path))

        with open(runner._master_bootstrap_script_path) as f:
            lines = [line.rstrip() for line in f]

        self.assertEqual(lines[0], '#!/bin/sh -ex')

        # check PWD gets stored
        self.assertIn('__mrjob_PWD=$PWD', lines)

        def assertScriptDownloads(path, name=None):
            uri = runner._upload_mgr.uri(path)
            name = runner._bootstrap_dir_mgr.name('file', path, name=name)

            self.assertIn(
                'hadoop fs -copyToLocal %s $__mrjob_PWD/%s' % (uri, name),
                lines)
            self.assertIn(
                'chmod a+x $__mrjob_PWD/%s' % (name,),
                lines)

        # check files get downloaded
        assertScriptDownloads(foo_py_path, 'bar.py')
        assertScriptDownloads('gs://walrus/scripts/ohnoes.sh')
        assertScriptDownloads('/tmp/quz', 'quz')
        assertScriptDownloads(runner._mrjob_tar_gz_path)
        assertScriptDownloads('speedups.sh')
        assertScriptDownloads('/tmp/s.sh')
        if PY2:
            assertScriptDownloads(yelpy_tar_gz_path)

        # check scripts get run

        # bootstrap
        self.assertIn(PYTHON_BIN + ' $__mrjob_PWD/bar.py', lines)
        self.assertIn('$__mrjob_PWD/ohnoes.sh', lines)
        # bootstrap_cmds
        self.assertIn('echo "Hi!"', lines)
        self.assertIn('true', lines)
        self.assertIn('ls', lines)
        # bootstrap_mrjob
        mrjob_tar_gz_name = runner._bootstrap_dir_mgr.name(
            'file', runner._mrjob_tar_gz_path)
        self.assertIn("__mrjob_PYTHON_LIB=$(" + PYTHON_BIN + " -c 'from"
                      " distutils.sysconfig import get_python_lib;"
                      " print(get_python_lib())')", lines)
        self.assertIn('sudo tar xfz $__mrjob_PWD/' + mrjob_tar_gz_name +
                      ' -C $__mrjob_PYTHON_LIB', lines)
        self.assertIn('sudo ' + PYTHON_BIN + ' -m compileall -f'
                      ' $__mrjob_PYTHON_LIB/mrjob && true', lines)
        # bootstrap_python_packages
        if PY2:
            self.assertIn('sudo apt-get install -y python-pip || '
                          'sudo yum install -y python-pip', lines)
            self.assertIn('sudo pip install $__mrjob_PWD/yelpy.tar.gz', lines)
        # bootstrap_scripts
        self.assertIn('$__mrjob_PWD/speedups.sh', lines)
        self.assertIn('$__mrjob_PWD/s.sh', lines)

    def test_no_bootstrap_script_if_not_needed(self):
        runner = DataprocJobRunner(conf_paths=[], bootstrap_mrjob=False,
                              bootstrap_python=False)

        runner._add_bootstrap_files_for_upload()
        self.assertIsNone(runner._master_bootstrap_script_path)

        # bootstrap actions don't figure into the master bootstrap script
        runner = DataprocJobRunner(conf_paths=[],
                              bootstrap_mrjob=False,
                              bootstrap_actions=['foo', 'bar baz'],
                              bootstrap_python=False,
                              pool_clusters=False)

        runner._add_bootstrap_files_for_upload()
        self.assertIsNone(runner._master_bootstrap_script_path)

        # using pooling doesn't require us to create a bootstrap script
        runner = DataprocJobRunner(conf_paths=[],
                              bootstrap_mrjob=False,
                              bootstrap_python=False,
                              pool_clusters=True)

        runner._add_bootstrap_files_for_upload()
        self.assertIsNone(runner._master_bootstrap_script_path)

    def test_bootstrap_actions_get_added(self):
        bootstrap_actions = [
            ('gs://elasticmapreduce/bootstrap-actions/configure-hadoop'
             ' -m,mapred.tasktracker.map.tasks.maximum=1'),
            'gs://foo/bar',
        ]

        runner = DataprocJobRunner(conf_paths=[],
                              bootstrap_actions=bootstrap_actions,
                              fs_sync_secs=0.00)

        cluster_id = runner.make_persistent_cluster()

        dataproc_conn = runner.make_dataproc_conn()
        actions = list(_yield_all_bootstrap_actions(dataproc_conn, cluster_id))

        self.assertEqual(len(actions), 3)

        self.assertEqual(
            actions[0].scriptpath,
            'gs://elasticmapreduce/bootstrap-actions/configure-hadoop')
        self.assertEqual(
            actions[0].args[0].value,
            '-m,mapred.tasktracker.map.tasks.maximum=1')
        self.assertEqual(actions[0].name, 'action 0')

        self.assertEqual(actions[1].scriptpath, 'gs://foo/bar')
        self.assertEqual(actions[1].args, [])
        self.assertEqual(actions[1].name, 'action 1')

        # check for master bootstrap script
        self.assertTrue(actions[2].scriptpath.startswith('gs://mrjob-'))
        self.assertTrue(actions[2].scriptpath.endswith('b.py'))
        self.assertEqual(actions[2].args, [])
        self.assertEqual(actions[2].name, 'master')

        # make sure master bootstrap script is on S3
        self.assertTrue(runner.fs.exists(actions[2].scriptpath))

    def test_bootstrap_mrjob_uses_python_bin(self):
        # use all the bootstrap options
        runner = DataprocJobRunner(conf_paths=[],
                              bootstrap_mrjob=True,
                              python_bin=['anaconda'])

        runner._add_bootstrap_files_for_upload()
        self.assertIsNotNone(runner._master_bootstrap_script_path)
        with open(runner._master_bootstrap_script_path, 'r') as f:
            content = f.read()

        self.assertIn('sudo anaconda -m compileall -f', content)

    def test_local_bootstrap_action(self):
        # make sure that local bootstrap action scripts get uploaded to S3
        action_path = os.path.join(self.tmp_dir, 'apt-install.sh')
        with open(action_path, 'w') as f:
            f.write('for $pkg in $@; do sudo apt-get install $pkg; done\n')

        bootstrap_actions = [
            action_path + ' python-scipy mysql-server']

        runner = DataprocJobRunner(conf_paths=[],
                              bootstrap_actions=bootstrap_actions,
                              fs_sync_secs=0.00)

        cluster_id = runner.make_persistent_cluster()

        dataproc_conn = runner.make_dataproc_conn()
        actions = list(_yield_all_bootstrap_actions(dataproc_conn, cluster_id))

        self.assertEqual(len(actions), 2)

        self.assertTrue(actions[0].scriptpath.startswith('gs://mrjob-'))
        self.assertTrue(actions[0].scriptpath.endswith('/apt-install.sh'))
        self.assertEqual(actions[0].name, 'action 0')
        self.assertEqual(actions[0].args[0].value, 'python-scipy')
        self.assertEqual(actions[0].args[1].value, 'mysql-server')

        # check for master boostrap script
        self.assertTrue(actions[1].scriptpath.startswith('gs://mrjob-'))
        self.assertTrue(actions[1].scriptpath.endswith('b.py'))
        self.assertEqual(actions[1].args, [])
        self.assertEqual(actions[1].name, 'master')

        # make sure master bootstrap script is on S3
        self.assertTrue(runner.fs.exists(actions[1].scriptpath))


class DataprocNoMapperTestCase(MockGoogleAPITestCase):

    def test_no_mapper(self):
        # read from STDIN, a local file, and a remote file
        stdin = BytesIO(b'foo\nbar\n')

        local_input_path = os.path.join(self.tmp_dir, 'input')
        with open(local_input_path, 'wb') as local_input_file:
            local_input_file.write(b'one fish two fish\nred fish blue fish\n')

        remote_input_path = 'gs://walrus/data/foo'
        self.put_gcs_data({'walrus': {'data/foo': b'foo\n'}})

        # setup fake output
        self.mock_dataproc_output = {('j-MOCKCLUSTER0', 1): [
            b'1\t["blue", "one", "red", "two"]\n',
            b'4\t["fish"]\n']}

        mr_job = MRNoMapper(['-r', 'dataproc', '-v',
                             '-', local_input_path, remote_input_path])
        mr_job.sandbox(stdin=stdin)

        results = []

        with mr_job.make_runner() as runner:
            runner.run()

            for line in runner.stream_output():
                key, value = mr_job.parse_output_line(line)
                results.append((key, value))

        self.assertEqual(sorted(results),
                          [(1, ['blue', 'one', 'red', 'two']),
                           (4, ['fish'])])


class MaxHoursIdleTestCase(MockGoogleAPITestCase):

    def assertRanIdleTimeoutScriptWith(self, runner, args):
        dataproc_conn = runner.make_dataproc_conn()
        cluster_id = runner.get_cluster_id()

        actions = list(_yield_all_bootstrap_actions(dataproc_conn, cluster_id))
        action = actions[-1]

        self.assertEqual(action.name, 'idle timeout')
        self.assertEqual(
            action.scriptpath,
            runner._upload_mgr.uri(_MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH))
        self.assertEqual([arg.value for arg in action.args], args)

    def assertDidNotUseIdleTimeoutScript(self, runner):
        dataproc_conn = runner.make_dataproc_conn()
        cluster_id = runner.get_cluster_id()

        actions = list(_yield_all_bootstrap_actions(dataproc_conn, cluster_id))
        action_names = [a.name for a in actions]

        self.assertNotIn('idle timeout', action_names)
        # idle timeout script should not even be uploaded
        self.assertNotIn(_MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH,
                         runner._upload_mgr.path_to_uri())

    def test_default(self):
        mr_job = MRWordCount(['-r', 'dataproc'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.run()
            self.assertDidNotUseIdleTimeoutScript(runner)

    def test_non_persistent_cluster(self):
        mr_job = MRWordCount(['-r', 'dataproc', '--max-hours-idle', '1'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.run()
            self.assertDidNotUseIdleTimeoutScript(runner)

    def test_persistent_cluster(self):
        mr_job = MRWordCount(['-r', 'dataproc', '--max-hours-idle', '0.01'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_cluster()
            self.assertRanIdleTimeoutScriptWith(runner, ['36', '300'])

    def test_mins_to_end_of_hour(self):
        mr_job = MRWordCount(['-r', 'dataproc', '--max-hours-idle', '1',
                              '--mins-to-end-of-hour', '10'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_cluster()
            self.assertRanIdleTimeoutScriptWith(runner, ['3600', '600'])

    def test_mins_to_end_of_hour_does_nothing_without_max_hours_idle(self):
        mr_job = MRWordCount(['-r', 'dataproc', '--mins-to-end-of-hour', '10'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_cluster()
            self.assertDidNotUseIdleTimeoutScript(runner)

    def test_use_integers(self):
        mr_job = MRWordCount(['-r', 'dataproc', '--max-hours-idle', '1.000001',
                              '--mins-to-end-of-hour', '10.000001'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.make_persistent_cluster()
            self.assertRanIdleTimeoutScriptWith(runner, ['3600', '600'])

    def pooled_clusters(self):
        mr_job = MRWordCount(['-r', 'dataproc', '--pool-clusters',
                              '--max-hours-idle', '0.5'])
        mr_job.sandbox()

        with mr_job.make_runner() as runner:
            runner.run()
            self.assertRanIdleTimeoutScriptWith(runner, ['1800', '300'])

    def test_bootstrap_script_is_actually_installed(self):
        self.assertTrue(os.path.exists(_MAX_HOURS_IDLE_BOOTSTRAP_ACTION_PATH))

class TestCatFallback(MockGoogleAPITestCase):

    def test_gcs_cat(self):
        self.put_gcs_data(
            {'walrus': {'one': b'one_text',
                        'two': b'two_text',
                        'three': b'three_text'}})

        runner = DataprocJobRunner(s3_tmp_dir='gs://walrus/tmp',
                              conf_paths=[])

        self.assertEqual(list(runner.fs.cat('gs://walrus/one')), [b'one_text'])

class CleanUpJobTestCase(MockGoogleAPITestCase):

    @contextmanager
    def _test_mode(self, mode):
        r = DataprocJobRunner(conf_paths=[])
        with patch.multiple(r,
                            _cleanup_cluster=mock.DEFAULT,
                            _cleanup_job=mock.DEFAULT,
                            _cleanup_local_tmp=mock.DEFAULT,
                            _cleanup_logs=mock.DEFAULT,
                            _cleanup_remote_tmp=mock.DEFAULT) as mock_dict:
            r.cleanup(mode=mode)
            yield mock_dict

    def _quick_runner(self):
        r = DataprocJobRunner(conf_paths=[], ec2_key_pair_file='fake.pem',
                         pool_clusters=True)
        r._cluster_id = 'j-ESSEOWENS'
        r._address = 'Albuquerque, NM'
        r._ran_job = False
        return r

    def test_cleanup_all(self):
        with self._test_mode('ALL') as m:
            self.assertFalse(m['_cleanup_cluster'].called)
            self.assertFalse(m['_cleanup_job'].called)
            self.assertTrue(m['_cleanup_local_tmp'].called)
            self.assertTrue(m['_cleanup_remote_tmp'].called)
            self.assertTrue(m['_cleanup_logs'].called)

    def test_cleanup_job(self):
        with self._test_mode('JOB') as m:
            self.assertFalse(m['_cleanup_cluster'].called)
            self.assertFalse(m['_cleanup_local_tmp'].called)
            self.assertFalse(m['_cleanup_remote_tmp'].called)
            self.assertFalse(m['_cleanup_logs'].called)
            self.assertFalse(m['_cleanup_job'].called)  # Only on failure

    def test_cleanup_none(self):
        with self._test_mode('NONE') as m:
            self.assertFalse(m['_cleanup_cluster'].called)
            self.assertFalse(m['_cleanup_local_tmp'].called)
            self.assertFalse(m['_cleanup_remote_tmp'].called)
            self.assertFalse(m['_cleanup_logs'].called)
            self.assertFalse(m['_cleanup_job'].called)

    def test_job_cleanup_mechanics_succeed(self):
        with no_handlers_for_logger():
            r = self._quick_runner()
            with patch.object(mrjob.dataproc, 'ssh_terminate_single_job') as m:
                r._cleanup_job()
            self.assertTrue(m.called)
            m.assert_any_call(['ssh'], 'Albuquerque, NM', 'fake.pem')

    def test_job_cleanup_mechanics_ssh_fail(self):
        def die_ssh(*args, **kwargs):
            raise IOError

        with no_handlers_for_logger('mrjob.dataproc'):
            r = self._quick_runner()
            stderr = StringIO()
            log_to_stream('mrjob.dataproc', stderr)
            with patch.object(mrjob.dataproc, 'ssh_terminate_single_job',
                              side_effect=die_ssh):
                r._cleanup_job()
                self.assertIn('Unable to kill job', stderr.getvalue())

    def test_job_cleanup_mechanics_not_started(self):
        r = self._quick_runner()
        r._cluster_id = None
        with patch.object(mrjob.dataproc, 'ssh_terminate_single_job') as p:
            r._cleanup_job()
            p.assert_not_called()

    def test_job_cleanup_mechanics_io_fail(self):
        def die_io(*args, **kwargs):
            raise IOError

        with no_handlers_for_logger('mrjob.dataproc'):
            r = self._quick_runner()
            with patch.object(mrjob.dataproc, 'ssh_terminate_single_job',
                              side_effect=die_io):
                stderr = StringIO()
                log_to_stream('mrjob.dataproc', stderr)
                r._cleanup_job()
                self.assertIn('Unable to kill job', stderr.getvalue())

    def test_dont_kill_if_successful(self):
        with no_handlers_for_logger('mrjob.dataproc'):
            r = self._quick_runner()
            with patch.object(mrjob.dataproc, 'ssh_terminate_single_job') as m:
                r._ran_job = True
                r._cleanup_job()
                m.assert_not_called()

    def test_kill_cluster(self):
        with no_handlers_for_logger('mrjob.dataproc'):
            r = self._quick_runner()
            with patch.object(mrjob.dataproc.DataprocJobRunner, 'make_dataproc_conn') as m:
                r._cleanup_cluster()
                self.assertTrue(m().terminate_jobflow.called)

    def test_kill_cluster_if_successful(self):
        # If they are setting up the cleanup to kill the cluster, mrjob should
        # kill the cluster independent of job success.
        with no_handlers_for_logger('mrjob.dataproc'):
            r = self._quick_runner()
            with patch.object(mrjob.dataproc.DataprocJobRunner, 'make_dataproc_conn') as m:
                r._ran_job = True
                r._cleanup_cluster()
                self.assertTrue(m().terminate_jobflow.called)

    def test_kill_persistent_cluster(self):
        with no_handlers_for_logger('mrjob.dataproc'):
            r = self._quick_runner()
            with patch.object(mrjob.dataproc.DataprocJobRunner, 'make_dataproc_conn') as m:
                r._opts['cluster_id'] = 'j-MOCKCLUSTER0'
                r._cleanup_cluster()
                self.assertTrue(m().terminate_jobflow.called)


class BootstrapPythonTestCase(MockGoogleAPITestCase):

    if PY2:
        EXPECTED_BOOTSTRAP = [
            ['sudo apt-get install -y python-pip || '
             'sudo yum install -y python-pip'],
             ['sudo pip install --upgrade ujson']]
    else:
        EXPECTED_BOOTSTRAP = [['sudo yum install -y python34']]

    def test_default(self):
        mr_job = MRTwoStepJob(['-r', 'dataproc'])
        with mr_job.make_runner() as runner:
            self.assertEqual(runner._opts['bootstrap_python'], True)
            self.assertEqual(runner._bootstrap_python(),
                             self.EXPECTED_BOOTSTRAP)
            self.assertEqual(runner._bootstrap,
                             self.EXPECTED_BOOTSTRAP)

    def test_bootstrap_python_switch(self):
        mr_job = MRTwoStepJob(['-r', 'dataproc', '--bootstrap-python'])

        with mr_job.make_runner() as runner:
            self.assertEqual(runner._opts['bootstrap_python'], True)
            self.assertEqual(runner._bootstrap_python(),
                             self.EXPECTED_BOOTSTRAP)
            self.assertEqual(runner._bootstrap,
                             self.EXPECTED_BOOTSTRAP)

    def test_no_bootstrap_python_switch(self):
        mr_job = MRTwoStepJob(['-r', 'dataproc', '--no-bootstrap-python'])
        with mr_job.make_runner() as runner:
            self.assertEqual(runner._opts['bootstrap_python'], False)
            self.assertEqual(runner._bootstrap_python(), [])
            self.assertEqual(runner._bootstrap, [])

    def _test_old_ami_version(self, ami_version):
        mr_job = MRTwoStepJob(['-r', 'dataproc', '--ami-version', ami_version])

        with no_handlers_for_logger('mrjob.dataproc'):
            stderr = StringIO()
            log_to_stream('mrjob.dataproc', stderr)

            with mr_job.make_runner() as runner:
                self.assertEqual(runner._opts['bootstrap_python'], True)
                self.assertEqual(runner._bootstrap_python(),
                             self.EXPECTED_BOOTSTRAP)
                self.assertEqual(runner._bootstrap,
                                 self.EXPECTED_BOOTSTRAP)

                if not PY2:
                    self.assertIn('will probably not work', stderr.getvalue())

    def test_ami_version_3_6_0(self):
        self._test_old_ami_version('3.6.0')

    def test_ami_version_latest(self):
        self._test_old_ami_version('latest')

    def test_bootstrap_python_comes_before_bootstrap(self):
        mr_job = MRTwoStepJob(['-r', 'dataproc', '--bootstrap', 'true'])

        with mr_job.make_runner() as runner:
            self.assertEqual(
                runner._bootstrap,
                self.EXPECTED_BOOTSTRAP + [['true']])

