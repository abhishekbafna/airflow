# -*- coding: utf-8 -*-
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import inspect
import os
import six
import shutil
import textwrap
import unittest
from datetime import datetime
from tempfile import NamedTemporaryFile, mkdtemp

from mock import patch, ANY
from freezegun import freeze_time

from airflow import models
from airflow.configuration import conf
from airflow.utils.dag_processing import SimpleTaskInstance
from airflow.models import DagModel, DagBag, TaskInstance as TI
from airflow.models.serialized_dag import SerializedDagModel
from airflow.utils.dates import timezone as tz
from airflow.utils.db import create_session
from airflow.utils.state import State
from airflow.utils.timezone import utc
from tests import cluster_policies
from tests.models import TEST_DAGS_FOLDER, DEFAULT_DATE
from tests.test_utils.asserts import assert_queries_count
from tests.test_utils.config import conf_vars
import airflow.example_dags


class DagBagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.empty_dir = mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.empty_dir)

    def test_get_existing_dag(self):
        """
        Test that we're able to parse some example DAGs and retrieve them
        """
        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=True)

        some_expected_dag_ids = ["example_bash_operator",
                                 "example_branch_operator"]

        for dag_id in some_expected_dag_ids:
            dag = dagbag.get_dag(dag_id)

            self.assertIsNotNone(dag)
            self.assertEqual(dag_id, dag.dag_id)

        self.assertGreaterEqual(dagbag.size(), 7)

    def test_get_non_existing_dag(self):
        """
        test that retrieving a non existing dag id returns None without crashing
        """
        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=False)

        non_existing_dag_id = "non_existing_dag_id"
        self.assertIsNone(dagbag.get_dag(non_existing_dag_id))

    def test_dont_load_example(self):
        """
        test that the example are not loaded
        """
        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=False)

        self.assertEqual(dagbag.size(), 0)

    def test_safe_mode_heuristic_match(self):
        """With safe mode enabled, a file matching the discovery heuristics
        should be discovered.
        """
        with NamedTemporaryFile(dir=self.empty_dir, suffix=".py") as fp:
            fp.write("# airflow".encode())
            fp.write("# DAG".encode())
            fp.flush()

            with conf_vars({('core', 'dags_folder'): self.empty_dir}):
                dagbag = models.DagBag(include_examples=False, safe_mode=True)

            self.assertEqual(len(dagbag.dagbag_stats), 1)
            self.assertEqual(
                dagbag.dagbag_stats[0].file,
                "/{}".format(os.path.basename(fp.name)))

    def test_safe_mode_heuristic_mismatch(self):
        """With safe mode enabled, a file not matching the discovery heuristics
        should not be discovered.
        """
        with NamedTemporaryFile(dir=self.empty_dir, suffix=".py"):
            with conf_vars({('core', 'dags_folder'): self.empty_dir}):
                dagbag = models.DagBag(include_examples=False, safe_mode=True)
            self.assertEqual(len(dagbag.dagbag_stats), 0)

    def test_safe_mode_disabled(self):
        """With safe mode disabled, an empty python file should be discovered.
        """
        with NamedTemporaryFile(dir=self.empty_dir, suffix=".py") as fp:
            with conf_vars({('core', 'dags_folder'): self.empty_dir}):
                dagbag = models.DagBag(include_examples=False, safe_mode=False)
            self.assertEqual(len(dagbag.dagbag_stats), 1)
            self.assertEqual(
                dagbag.dagbag_stats[0].file,
                "/{}".format(os.path.basename(fp.name)))

    def test_process_file_that_contains_multi_bytes_char(self):
        """
        test that we're able to parse file that contains multi-byte char
        """
        f = NamedTemporaryFile()
        f.write('\u3042'.encode('utf8'))  # write multi-byte char (hiragana)
        f.flush()

        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=False)
        self.assertEqual([], dagbag.process_file(f.name))

    def test_zip_skip_log(self):
        """
        test the loading of a DAG from within a zip file that skips another file because
        it doesn't have "airflow" and "DAG"
        """
        from mock import Mock
        with patch('airflow.models.DagBag.log') as log_mock:
            log_mock.info = Mock()
            test_zip_path = os.path.join(TEST_DAGS_FOLDER, "test_zip.zip")
            dagbag = models.DagBag(dag_folder=test_zip_path, include_examples=False)

            self.assertTrue(dagbag.has_logged)
            log_mock.info.assert_any_call("File %s assumed to contain no DAGs. Skipping.",
                                          test_zip_path)

    def test_zip(self):
        """
        test the loading of a DAG within a zip file that includes dependencies
        """
        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=False)
        dagbag.process_file(os.path.join(TEST_DAGS_FOLDER, "test_zip.zip"))
        self.assertTrue(dagbag.get_dag("test_zip_dag"))

    def test_process_file_cron_validity_check(self):
        """
        test if an invalid cron expression
        as schedule interval can be identified
        """
        invalid_dag_files = ["test_invalid_cron.py", "test_zip_invalid_cron.zip"]
        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=False)

        self.assertEqual(len(dagbag.import_errors), 0)
        for d in invalid_dag_files:
            dagbag.process_file(os.path.join(TEST_DAGS_FOLDER, d))
        self.assertEqual(len(dagbag.import_errors), len(invalid_dag_files))
        self.assertEqual(len(dagbag.dags), 0)

    @patch.object(DagModel, 'get_current')
    def test_get_dag_without_refresh(self, mock_dagmodel):
        """
        Test that, once a DAG is loaded, it doesn't get refreshed again if it
        hasn't been expired.
        """
        dag_id = 'example_bash_operator'

        mock_dagmodel.return_value = DagModel()
        mock_dagmodel.return_value.last_expired = None
        mock_dagmodel.return_value.fileloc = 'foo'

        class TestDagBag(models.DagBag):
            process_file_calls = 0

            def process_file(self, filepath, only_if_updated=True, safe_mode=True):
                if 'example_bash_operator.py' == os.path.basename(filepath):
                    TestDagBag.process_file_calls += 1
                super(TestDagBag, self).process_file(filepath, only_if_updated, safe_mode)

        dagbag = TestDagBag(include_examples=True)
        dagbag.process_file_calls

        # Should not call process_file again, since it's already loaded during init.
        self.assertEqual(1, dagbag.process_file_calls)
        self.assertIsNotNone(dagbag.get_dag(dag_id))
        self.assertEqual(1, dagbag.process_file_calls)

    def test_get_dag_fileloc(self):
        """
        Test that fileloc is correctly set when we load example DAGs,
        specifically SubDAGs and packaged DAGs.
        """
        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=True)
        dagbag.process_file(os.path.join(TEST_DAGS_FOLDER, "test_zip.zip"))

        expected = {
            'example_bash_operator': 'airflow/example_dags/example_bash_operator.py',
            'example_subdag_operator': 'airflow/example_dags/example_subdag_operator.py',
            'example_subdag_operator.section-1': 'airflow/example_dags/subdags/subdag.py',
            'test_zip_dag': 'dags/test_zip.zip/test_zip.py'
        }

        for dag_id, path in expected.items():
            dag = dagbag.get_dag(dag_id)
            self.assertTrue(dag.fileloc.endswith(path))

    @patch.object(DagModel, "get_current")
    def test_refresh_py_dag(self, mock_dagmodel):
        """
        Test that we can refresh an ordinary .py DAG
        """
        EXAMPLE_DAGS_FOLDER = airflow.example_dags.__path__[0]

        dag_id = "example_bash_operator"
        fileloc = os.path.realpath(
            os.path.join(EXAMPLE_DAGS_FOLDER, "example_bash_operator.py")
        )

        mock_dagmodel.return_value = DagModel()
        mock_dagmodel.return_value.last_expired = datetime.max.replace(
            tzinfo=utc
        )
        mock_dagmodel.return_value.fileloc = fileloc

        class TestDagBag(DagBag):
            process_file_calls = 0

            def process_file(self, filepath, only_if_updated=True, safe_mode=True):
                if filepath == fileloc:
                    TestDagBag.process_file_calls += 1
                return super(TestDagBag, self).process_file(filepath, only_if_updated, safe_mode)

        dagbag = TestDagBag(dag_folder=self.empty_dir, include_examples=True)

        self.assertEqual(1, dagbag.process_file_calls)
        dag = dagbag.get_dag(dag_id)
        self.assertIsNotNone(dag)
        self.assertEqual(dag_id, dag.dag_id)
        self.assertEqual(2, dagbag.process_file_calls)

    @patch.object(DagModel, "get_current")
    def test_refresh_packaged_dag(self, mock_dagmodel):
        """
        Test that we can refresh a packaged DAG
        """
        dag_id = "test_zip_dag"
        fileloc = os.path.realpath(
            os.path.join(TEST_DAGS_FOLDER, "test_zip.zip/test_zip.py")
        )

        mock_dagmodel.return_value = DagModel()
        mock_dagmodel.return_value.last_expired = datetime.max.replace(
            tzinfo=utc
        )
        mock_dagmodel.return_value.fileloc = fileloc

        class TestDagBag(DagBag):
            process_file_calls = 0

            def process_file(self, filepath, only_if_updated=True, safe_mode=True):
                if filepath in fileloc:
                    TestDagBag.process_file_calls += 1
                return super(TestDagBag, self).process_file(filepath, only_if_updated, safe_mode)

        dagbag = TestDagBag(dag_folder=os.path.realpath(TEST_DAGS_FOLDER), include_examples=False)

        self.assertEqual(1, dagbag.process_file_calls)
        dag = dagbag.get_dag(dag_id)
        self.assertIsNotNone(dag)
        self.assertEqual(dag_id, dag.dag_id)
        self.assertEqual(2, dagbag.process_file_calls)

    def process_dag(self, create_dag):
        """
        Helper method to process a file generated from the input create_dag function.
        """
        # write source to file
        source = textwrap.dedent(''.join(
            inspect.getsource(create_dag).splitlines(True)[1:-1]))
        f = NamedTemporaryFile()
        f.write(source.encode('utf8'))
        f.flush()

        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=False)
        found_dags = dagbag.process_file(f.name)
        return dagbag, found_dags, f.name

    def validate_dags(self, expected_parent_dag, actual_found_dags, actual_dagbag,
                      should_be_found=True):
        expected_dag_ids = list(map(lambda dag: dag.dag_id, expected_parent_dag.subdags))
        expected_dag_ids.append(expected_parent_dag.dag_id)

        actual_found_dag_ids = list(map(lambda dag: dag.dag_id, actual_found_dags))

        for dag_id in expected_dag_ids:
            actual_dagbag.log.info('validating %s' % dag_id)
            self.assertEqual(
                dag_id in actual_found_dag_ids, should_be_found,
                'dag "%s" should %shave been found after processing dag "%s"' %
                (dag_id, '' if should_be_found else 'not ', expected_parent_dag.dag_id)
            )
            self.assertEqual(
                dag_id in actual_dagbag.dags, should_be_found,
                'dag "%s" should %sbe in dagbag.dags after processing dag "%s"' %
                (dag_id, '' if should_be_found else 'not ', expected_parent_dag.dag_id)
            )

    def test_load_subdags(self):
        # Define Dag to load
        def standard_subdag():
            from airflow.models import DAG
            from airflow.operators.dummy_operator import DummyOperator
            from airflow.operators.subdag_operator import SubDagOperator
            import datetime
            DAG_NAME = 'master'
            DEFAULT_ARGS = {
                'owner': 'owner1',
                'start_date': datetime.datetime(2016, 1, 1)
            }
            dag = DAG(
                DAG_NAME,
                default_args=DEFAULT_ARGS)

            # master:
            #     A -> opSubDag_0
            #          master.opsubdag_0:
            #              -> subdag_0.task
            #     A -> opSubDag_1
            #          master.opsubdag_1:
            #              -> subdag_1.task

            with dag:
                def subdag_0():
                    subdag_0 = DAG('master.opSubdag_0', default_args=DEFAULT_ARGS)
                    DummyOperator(task_id='subdag_0.task', dag=subdag_0)
                    return subdag_0

                def subdag_1():
                    subdag_1 = DAG('master.opSubdag_1', default_args=DEFAULT_ARGS)
                    DummyOperator(task_id='subdag_1.task', dag=subdag_1)
                    return subdag_1

                opSubdag_0 = SubDagOperator(
                    task_id='opSubdag_0', dag=dag, subdag=subdag_0())
                opSubdag_1 = SubDagOperator(
                    task_id='opSubdag_1', dag=dag, subdag=subdag_1())

                opA = DummyOperator(task_id='A')
                opA.set_downstream(opSubdag_0)
                opA.set_downstream(opSubdag_1)
            return dag

        testDag = standard_subdag()
        # sanity check to make sure DAG.subdag is still functioning properly
        self.assertEqual(len(testDag.subdags), 2)

        # Perform processing dag
        dagbag, found_dags, _ = self.process_dag(standard_subdag)

        # Validate correctness
        # all dags from testDag should be listed
        self.validate_dags(testDag, found_dags, dagbag)

        # Define Dag to load
        def nested_subdags():
            from airflow.models import DAG
            from airflow.operators.dummy_operator import DummyOperator
            from airflow.operators.subdag_operator import SubDagOperator
            import datetime
            DAG_NAME = 'master'
            DEFAULT_ARGS = {
                'owner': 'owner1',
                'start_date': datetime.datetime(2016, 1, 1)
            }
            dag = DAG(
                DAG_NAME,
                default_args=DEFAULT_ARGS)

            # master:
            #     A -> opSubdag_0
            #          master.opSubdag_0:
            #              -> opSubDag_A
            #                 master.opSubdag_0.opSubdag_A:
            #                     -> subdag_A.task
            #              -> opSubdag_B
            #                 master.opSubdag_0.opSubdag_B:
            #                     -> subdag_B.task
            #     A -> opSubdag_1
            #          master.opSubdag_1:
            #              -> opSubdag_C
            #                 master.opSubdag_1.opSubdag_C:
            #                     -> subdag_C.task
            #              -> opSubDag_D
            #                 master.opSubdag_1.opSubdag_D:
            #                     -> subdag_D.task

            with dag:
                def subdag_A():
                    subdag_A = DAG(
                        'master.opSubdag_0.opSubdag_A', default_args=DEFAULT_ARGS)
                    DummyOperator(task_id='subdag_A.task', dag=subdag_A)
                    return subdag_A

                def subdag_B():
                    subdag_B = DAG(
                        'master.opSubdag_0.opSubdag_B', default_args=DEFAULT_ARGS)
                    DummyOperator(task_id='subdag_B.task', dag=subdag_B)
                    return subdag_B

                def subdag_C():
                    subdag_C = DAG(
                        'master.opSubdag_1.opSubdag_C', default_args=DEFAULT_ARGS)
                    DummyOperator(task_id='subdag_C.task', dag=subdag_C)
                    return subdag_C

                def subdag_D():
                    subdag_D = DAG(
                        'master.opSubdag_1.opSubdag_D', default_args=DEFAULT_ARGS)
                    DummyOperator(task_id='subdag_D.task', dag=subdag_D)
                    return subdag_D

                def subdag_0():
                    subdag_0 = DAG('master.opSubdag_0', default_args=DEFAULT_ARGS)
                    SubDagOperator(task_id='opSubdag_A', dag=subdag_0, subdag=subdag_A())
                    SubDagOperator(task_id='opSubdag_B', dag=subdag_0, subdag=subdag_B())
                    return subdag_0

                def subdag_1():
                    subdag_1 = DAG('master.opSubdag_1', default_args=DEFAULT_ARGS)
                    SubDagOperator(task_id='opSubdag_C', dag=subdag_1, subdag=subdag_C())
                    SubDagOperator(task_id='opSubdag_D', dag=subdag_1, subdag=subdag_D())
                    return subdag_1

                opSubdag_0 = SubDagOperator(
                    task_id='opSubdag_0', dag=dag, subdag=subdag_0())
                opSubdag_1 = SubDagOperator(
                    task_id='opSubdag_1', dag=dag, subdag=subdag_1())

                opA = DummyOperator(task_id='A')
                opA.set_downstream(opSubdag_0)
                opA.set_downstream(opSubdag_1)

            return dag

        testDag = nested_subdags()
        # sanity check to make sure DAG.subdag is still functioning properly
        self.assertEqual(len(testDag.subdags), 6)

        # Perform processing dag
        dagbag, found_dags, _ = self.process_dag(nested_subdags)

        # Validate correctness
        # all dags from testDag should be listed
        self.validate_dags(testDag, found_dags, dagbag)

    def test_skip_cycle_dags(self):
        """
        Don't crash when loading an invalid (contains a cycle) DAG file.
        Don't load the dag into the DagBag either
        """

        # Define Dag to load
        def basic_cycle():
            from airflow.models import DAG
            from airflow.operators.dummy_operator import DummyOperator
            import datetime
            DAG_NAME = 'cycle_dag'
            DEFAULT_ARGS = {
                'owner': 'owner1',
                'start_date': datetime.datetime(2016, 1, 1)
            }
            dag = DAG(
                DAG_NAME,
                default_args=DEFAULT_ARGS)

            # A -> A
            with dag:
                opA = DummyOperator(task_id='A')
                opA.set_downstream(opA)

            return dag

        testDag = basic_cycle()
        # sanity check to make sure DAG.subdag is still functioning properly
        self.assertEqual(len(testDag.subdags), 0)

        # Perform processing dag
        dagbag, found_dags, file_path = self.process_dag(basic_cycle)

        # #Validate correctness
        # None of the dags should be found
        self.validate_dags(testDag, found_dags, dagbag, should_be_found=False)
        self.assertIn(file_path, dagbag.import_errors)

        # Define Dag to load
        def nested_subdag_cycle():
            from airflow.models import DAG
            from airflow.operators.dummy_operator import DummyOperator
            from airflow.operators.subdag_operator import SubDagOperator
            import datetime
            DAG_NAME = 'nested_cycle'
            DEFAULT_ARGS = {
                'owner': 'owner1',
                'start_date': datetime.datetime(2016, 1, 1)
            }
            dag = DAG(
                DAG_NAME,
                default_args=DEFAULT_ARGS)

            # cycle:
            #     A -> opSubdag_0
            #          cycle.opSubdag_0:
            #              -> opSubDag_A
            #                 cycle.opSubdag_0.opSubdag_A:
            #                     -> subdag_A.task
            #              -> opSubdag_B
            #                 cycle.opSubdag_0.opSubdag_B:
            #                     -> subdag_B.task
            #     A -> opSubdag_1
            #          cycle.opSubdag_1:
            #              -> opSubdag_C
            #                 cycle.opSubdag_1.opSubdag_C:
            #                     -> subdag_C.task -> subdag_C.task  >Invalid Loop<
            #              -> opSubDag_D
            #                 cycle.opSubdag_1.opSubdag_D:
            #                     -> subdag_D.task

            with dag:
                def subdag_A():
                    subdag_A = DAG(
                        'nested_cycle.opSubdag_0.opSubdag_A', default_args=DEFAULT_ARGS)
                    DummyOperator(task_id='subdag_A.task', dag=subdag_A)
                    return subdag_A

                def subdag_B():
                    subdag_B = DAG(
                        'nested_cycle.opSubdag_0.opSubdag_B', default_args=DEFAULT_ARGS)
                    DummyOperator(task_id='subdag_B.task', dag=subdag_B)
                    return subdag_B

                def subdag_C():
                    subdag_C = DAG(
                        'nested_cycle.opSubdag_1.opSubdag_C', default_args=DEFAULT_ARGS)
                    opSubdag_C_task = DummyOperator(
                        task_id='subdag_C.task', dag=subdag_C)
                    # introduce a loop in opSubdag_C
                    opSubdag_C_task.set_downstream(opSubdag_C_task)
                    return subdag_C

                def subdag_D():
                    subdag_D = DAG(
                        'nested_cycle.opSubdag_1.opSubdag_D', default_args=DEFAULT_ARGS)
                    DummyOperator(task_id='subdag_D.task', dag=subdag_D)
                    return subdag_D

                def subdag_0():
                    subdag_0 = DAG('nested_cycle.opSubdag_0', default_args=DEFAULT_ARGS)
                    SubDagOperator(task_id='opSubdag_A', dag=subdag_0, subdag=subdag_A())
                    SubDagOperator(task_id='opSubdag_B', dag=subdag_0, subdag=subdag_B())
                    return subdag_0

                def subdag_1():
                    subdag_1 = DAG('nested_cycle.opSubdag_1', default_args=DEFAULT_ARGS)
                    SubDagOperator(task_id='opSubdag_C', dag=subdag_1, subdag=subdag_C())
                    SubDagOperator(task_id='opSubdag_D', dag=subdag_1, subdag=subdag_D())
                    return subdag_1

                opSubdag_0 = SubDagOperator(
                    task_id='opSubdag_0', dag=dag, subdag=subdag_0())
                opSubdag_1 = SubDagOperator(
                    task_id='opSubdag_1', dag=dag, subdag=subdag_1())

                opA = DummyOperator(task_id='A')
                opA.set_downstream(opSubdag_0)
                opA.set_downstream(opSubdag_1)

            return dag

        testDag = nested_subdag_cycle()
        # sanity check to make sure DAG.subdag is still functioning properly
        self.assertEqual(len(testDag.subdags), 6)

        # Perform processing dag
        dagbag, found_dags, file_path = self.process_dag(nested_subdag_cycle)

        # Validate correctness
        # None of the dags should be found
        self.validate_dags(testDag, found_dags, dagbag, should_be_found=False)
        self.assertIn(file_path, dagbag.import_errors)

    def test_process_file_with_none(self):
        """
        test that process_file can handle Nones
        """
        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=False)

        self.assertEqual([], dagbag.process_file(None))

    @patch.object(TI, 'handle_failure')
    def test_kill_zombies(self, mock_ti_handle_failure):
        """
        Test that kill zombies call TIs failure handler with proper context
        """
        dagbag = models.DagBag(dag_folder=self.empty_dir, include_examples=True)
        with create_session() as session:
            session.query(TI).delete()
            dag = dagbag.get_dag('example_branch_operator')
            task = dag.get_task(task_id='run_this_first')

            ti = TI(task, DEFAULT_DATE, State.RUNNING)

            session.add(ti)
            session.commit()

            zombies = [SimpleTaskInstance(ti)]
            dagbag.kill_zombies(zombies)
            mock_ti_handle_failure \
                .assert_called_with(ANY,
                                    conf.getboolean('core', 'unit_test_mode'),
                                    ANY)

    def test_deactivate_unknown_dags(self):
        """
        Test that dag_ids not passed into deactivate_unknown_dags
        are deactivated when function is invoked
        """
        dagbag = DagBag(include_examples=True)
        dag_id = "test_deactivate_unknown_dags"
        expected_active_dags = dagbag.dags.keys()

        model_before = DagModel(dag_id=dag_id, is_active=True)
        with create_session() as session:
            session.merge(model_before)

        models.DAG.deactivate_unknown_dags(expected_active_dags)

        after_model = DagModel.get_dagmodel(dag_id)
        self.assertTrue(model_before.is_active)
        self.assertFalse(after_model.is_active)

        # clean up
        with create_session() as session:
            session.query(DagModel).filter(DagModel.dag_id == 'test_deactivate_unknown_dags').delete()

    @patch("airflow.models.dagbag.settings.STORE_SERIALIZED_DAGS", True)
    @patch("airflow.models.dagbag.settings.MIN_SERIALIZED_DAG_UPDATE_INTERVAL", 5)
    @patch("airflow.models.dagbag.settings.MIN_SERIALIZED_DAG_FETCH_INTERVAL", 5)
    def test_get_dag_with_dag_serialization(self):
        """
        Test that Serialized DAG is updated in DagBag when it is updated in
        Serialized DAG table after 'min_serialized_dag_fetch_interval' seconds are passed.
        """

        with freeze_time(tz.datetime(2020, 1, 5, 0, 0, 0)):
            example_bash_op_dag = DagBag(include_examples=True).dags.get("example_bash_operator")
            SerializedDagModel.write_dag(dag=example_bash_op_dag)

            dag_bag = DagBag(store_serialized_dags=True)
            ser_dag_1 = dag_bag.get_dag("example_bash_operator")
            ser_dag_1_update_time = dag_bag.dags_last_fetched["example_bash_operator"]
            self.assertEqual(example_bash_op_dag.tags, ser_dag_1.tags)
            self.assertEqual(ser_dag_1_update_time, tz.datetime(2020, 1, 5, 0, 0, 0))

        # Check that if min_serialized_dag_fetch_interval has not passed we do not fetch the DAG
        # from DB
        with freeze_time(tz.datetime(2020, 1, 5, 0, 0, 4)):
            with assert_queries_count(0):
                self.assertEqual(dag_bag.get_dag("example_bash_operator").tags, ["example"])

        # Make a change in the DAG and write Serialized DAG to the DB
        with freeze_time(tz.datetime(2020, 1, 5, 0, 0, 6)):
            example_bash_op_dag.tags += ["new_tag"]
            SerializedDagModel.write_dag(dag=example_bash_op_dag)

        # Since min_serialized_dag_fetch_interval is passed verify that calling 'dag_bag.get_dag'
        # fetches the Serialized DAG from DB
        with freeze_time(tz.datetime(2020, 1, 5, 0, 0, 8)):
            with assert_queries_count(2):
                updated_ser_dag_1 = dag_bag.get_dag("example_bash_operator")
                updated_ser_dag_1_update_time = dag_bag.dags_last_fetched["example_bash_operator"]

        six.assertCountEqual(self, updated_ser_dag_1.tags, ["example", "new_tag"])
        self.assertGreater(updated_ser_dag_1_update_time, ser_dag_1_update_time)

    @patch("airflow.settings.policy", cluster_policies.cluster_policy)
    def test_cluster_policy_violation(self):
        """test that file processing results in import error when task does not
        obey cluster policy.
        """
        dag_file = os.path.join(TEST_DAGS_FOLDER, "test_missing_owner.py")

        dagbag = DagBag(dag_folder=dag_file)
        self.assertEqual(set(), set(dagbag.dag_ids))
        expected_import_errors = {
            dag_file: (
                "DAG policy violation (DAG ID: test_missing_owner, Path: {}):\n"
                "Notices:\n"
                " * Task must have non-None non-default owner. Current value: airflow".format(dag_file)
            )
        }
        self.maxDiff = None
        self.assertEqual(expected_import_errors, dagbag.import_errors)

    @patch("airflow.settings.policy", cluster_policies.cluster_policy)
    def test_cluster_policy_obeyed(self):
        """test that dag successfully imported without import errors when tasks
        obey cluster policy.
        """
        dag_file = os.path.join(TEST_DAGS_FOLDER, "test_with_non_default_owner.py")

        dagbag = DagBag(dag_folder=dag_file)
        self.assertEqual({"test_with_non_default_owner"}, set(dagbag.dag_ids))

        self.assertEqual({}, dagbag.import_errors)
