# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Garbage collection service which deletes old data from the database.
"""

import signal
import datetime
import random

import eventlet
from eventlet.support import greenlets as greenlet
from oslo_config import cfg

from st2common import log as logging
from st2common.constants.exit_codes import SUCCESS_EXIT_CODE
from st2common.constants.exit_codes import FAILURE_EXIT_CODE
from st2common.constants.garbage_collection import DEFAULT_COLLECTION_INTERVAL
from st2common.constants.garbage_collection import DEFAULT_SLEEP_DELAY
from st2common.constants.garbage_collection import MINIMUM_TTL_DAYS
from st2common.constants.garbage_collection import MINIMUM_TTL_DAYS_EXECUTION_OUTPUT
from st2common.util import isotime
from st2common.util.date import get_datetime_utc_now
from st2common.garbage_collection.executions import purge_executions
from st2common.garbage_collection.executions import purge_execution_output_objects
from st2common.garbage_collection.inquiries import purge_inquiries
from st2common.garbage_collection.trigger_instances import purge_trigger_instances

__all__ = [
    'GarbageCollectorService'
]

LOG = logging.getLogger(__name__)


class GarbageCollectorService(object):
    def __init__(self, collection_interval=DEFAULT_COLLECTION_INTERVAL,
                 sleep_delay=DEFAULT_SLEEP_DELAY):
        """
        :param collection_interval: How often to check database for old data and perform garbage
               collection.
        :type collection_interval: ``int``

        :param sleep_delay: How long to sleep (in seconds) between collection of different object
                            types.
        :type sleep_delay: ``int``
        """
        self._collection_interval = collection_interval

        self._action_executions_ttl = cfg.CONF.garbagecollector.action_executions_ttl
        self._action_executions_output_ttl = cfg.CONF.garbagecollector.action_executions_output_ttl
        self._trigger_instances_ttl = cfg.CONF.garbagecollector.trigger_instances_ttl
        self._purge_inquiries = cfg.CONF.garbagecollector.purge_inquiries

        self._validate_ttl_values()

        self._sleep_delay = sleep_delay

    def run(self):
        self._running = True

        self._register_signal_handlers()

        # Wait a couple of seconds before performing initial collection to prevent thundering herd
        # effect when restarting multiple services at the same time
        jitter_seconds = random.uniform(0, 3)
        eventlet.sleep(jitter_seconds)

        try:
            self._main_loop()
        except greenlet.GreenletExit:
            self._running = False
            return SUCCESS_EXIT_CODE
        except Exception as e:
            LOG.exception('Exception in the garbage collector: %s' % (str(e)))
            self._running = False
            return FAILURE_EXIT_CODE

        return SUCCESS_EXIT_CODE

    def _register_signal_handlers(self):
        signal.signal(signal.SIGUSR2, self.handle_sigusr2)

    def handle_sigusr2(self, signal_number, stack_frame):
        LOG.info('Forcing garbage collection...')
        self._perform_garbage_collection()

    def shutdown(self):
        self._running = False

    def _main_loop(self):
        while self._running:
            self._perform_garbage_collection()

            LOG.info('Sleeping for %s seconds before next garbage collection...' %
                     (self._collection_interval))
            eventlet.sleep(self._collection_interval)

    def _validate_ttl_values(self):
        """
        Validate that a user has supplied reasonable TTL values.
        """
        if self._action_executions_ttl and self._action_executions_ttl < MINIMUM_TTL_DAYS:
            raise ValueError('Minimum possible TTL for action_executions_ttl in days is %s' %
                             (MINIMUM_TTL_DAYS))

        if self._trigger_instances_ttl and self._trigger_instances_ttl < MINIMUM_TTL_DAYS:
            raise ValueError('Minimum possible TTL for trigger_instances_ttl in days is %s' %
                             (MINIMUM_TTL_DAYS))

        if self._action_executions_output_ttl and \
                self._action_executions_output_ttl < MINIMUM_TTL_DAYS_EXECUTION_OUTPUT:
            raise ValueError(('Minimum possible TTL for action_executions_output_ttl in days '
                              'is %s') % (MINIMUM_TTL_DAYS_EXECUTION_OUTPUT))

    def _perform_garbage_collection(self):
        LOG.info('Performing garbage collection...')

        # Note: We sleep for a bit between garbage collection of each object type to prevent busy
        # waiting
        if self._action_executions_ttl >= MINIMUM_TTL_DAYS:
            self._purge_action_executions()
            eventlet.sleep(self._sleep_delay)
        else:
            LOG.debug('Skipping garbage collection for action executions since it\'s not '
                      'configured')

        if self._action_executions_output_ttl >= MINIMUM_TTL_DAYS_EXECUTION_OUTPUT:
            self._purge_action_executions_output()
            eventlet.sleep(self._sleep_delay)
        else:
            LOG.debug('Skipping garbage collection for action executions output since it\'s not '
                      'configured')

        if self._trigger_instances_ttl >= MINIMUM_TTL_DAYS:
            self._purge_trigger_instances()
            eventlet.sleep(self._sleep_delay)
        else:
            LOG.debug('Skipping garbage collection for trigger instances since it\'s not '
                      'configured')

        if self._purge_inquiries:
            self._timeout_inquiries()
            eventlet.sleep(self._sleep_delay)
        else:
            LOG.debug('Skipping garbage collection for Inquiries since it\'s not '
                      'configured')

    def _purge_action_executions(self):
        """
        Purge action executions and corresponding live action, stdout and stderr object which match
        the criteria defined in the config.
        """
        LOG.info('Performing garbage collection for action executions and related objects')

        utc_now = get_datetime_utc_now()
        timestamp = (utc_now - datetime.timedelta(days=self._action_executions_ttl))

        # Another sanity check to make sure we don't delete new executions
        if timestamp > (utc_now - datetime.timedelta(days=MINIMUM_TTL_DAYS)):
            raise ValueError('Calculated timestamp would violate the minimum TTL constraint')

        timestamp_str = isotime.format(dt=timestamp)
        LOG.info('Deleting action executions older than: %s' % (timestamp_str))

        assert timestamp < utc_now

        try:
            purge_executions(logger=LOG, timestamp=timestamp)
        except Exception as e:
            LOG.exception('Failed to delete executions: %s' % (str(e)))

        return True

    def _purge_action_executions_output(self):
        LOG.info('Performing garbage collection for action executions output objects')

        utc_now = get_datetime_utc_now()
        timestamp = (utc_now - datetime.timedelta(days=self._action_executions_output_ttl))

        # Another sanity check to make sure we don't delete new objects
        if timestamp > (utc_now - datetime.timedelta(days=MINIMUM_TTL_DAYS_EXECUTION_OUTPUT)):
            raise ValueError('Calculated timestamp would violate the minimum TTL constraint')

        timestamp_str = isotime.format(dt=timestamp)
        LOG.info('Deleting action executions output objects older than: %s' % (timestamp_str))

        assert timestamp < utc_now

        try:
            purge_execution_output_objects(logger=LOG, timestamp=timestamp)
        except Exception as e:
            LOG.exception('Failed to delete execution output objects: %s' % (str(e)))

        return True

    def _purge_trigger_instances(self):
        """
        Purge trigger instances which match the criteria defined in the config.
        """
        LOG.info('Performing garbage collection for trigger instances')

        utc_now = get_datetime_utc_now()
        timestamp = (utc_now - datetime.timedelta(days=self._trigger_instances_ttl))

        # Another sanity check to make sure we don't delete new executions
        if timestamp > (utc_now - datetime.timedelta(days=MINIMUM_TTL_DAYS)):
            raise ValueError('Calculated timestamp would violate the minimum TTL constraint')

        timestamp_str = isotime.format(dt=timestamp)
        LOG.info('Deleting trigger instances older than: %s' % (timestamp_str))

        assert timestamp < utc_now

        try:
            purge_trigger_instances(logger=LOG, timestamp=timestamp)
        except Exception as e:
            LOG.exception('Failed to trigger instances: %s' % (str(e)))

        return True

    def _timeout_inquiries(self):
        """Mark Inquiries as "timeout" that have exceeded their TTL
        """
        LOG.info('Performing garbage collection for Inquiries')

        try:
            purge_inquiries(logger=LOG)
        except Exception as e:
            LOG.exception('Failed to purge inquiries: %s' % (str(e)))

        return True
