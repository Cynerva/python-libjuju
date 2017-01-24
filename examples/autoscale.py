#!/usr/bin/env python3.5

"""
Juju auto-scaler demo.


How to run it:

# prepare a new Juju model, bootstrapping if necessary
juju bootstrap lxd demo
juju add-model demo

# deploy the scalable wiki bundle from the charmstore
juju deploy wiki-scalable

# clone and build libjuju
git clone https://github.com/juju/python-libjuju.git
cd python-libjuju
make .tox

# create a sample scaling config file
cat > ~/wiki-scaler.yaml <<EOF
wiki:
  min-units: 1
  max-units: 10
  alarms:
    high-load:
      statistic: average
      metric: cpu
      comparator: '>='
      threshold: .80
      period: 300
      policies:
        add-capacity:
          scaling-adjustment: 30
          adjustment-type: percent
          warmup: 360
    low-load:
      statistic: average
      metric: cpu
      comparator: '<='
      threshold: .50
      period: 300
      policies:
        decrease-capacity:
          scaling-adjustment: -1
          adjustment-type: units
EOF

# start the auto-scaler
.tox/py35/bin/python3.5 examples/autoscale.py ~/wiki-scaler.yaml

# apply load to wiki to force scale out
sudo apt install apache2-utils
sudo apt update
ab -k -c 1000 -n 5000 http://ip-of-loadbalancer-unit-from-juju-status/
# note: trailing slash in the url is important ^

# wiki units will be added to accomodate the increased load
# when load diminishes, wiki units will be removed

"""

import argparse
import asyncio
import datetime
import logging
import signal
import sys
import textwrap

from concurrent.futures import CancelledError

import yaml

from juju.model import Model, ModelObserver

log = logging.getLogger(__name__)


def setup_parser():
    """Setup parser for cmdline args.

    """
    parser = argparse.ArgumentParser(
        prog='autoscale',
        description=textwrap.dedent(__doc__),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument(
        'config', type=argparse.FileType('r', encoding='utf-8'))
    parser.add_argument(
        '-l', '--log-level',
        choices=('INFO', 'DEBUG', 'WARN', 'ERROR', 'CRITICAL'),
        default='DEBUG',
    )

    return parser


async def cpu_metric(unit, **kw):
    try:
        action = await unit.run('cat /proc/loadavg')
    except Exception:
        return None
    loadavg = action.results.get('Stdout')
    five_min_load = loadavg.strip().split()[1]
    log.debug('5min load on %s is %s', unit.name, five_min_load)
    return float(five_min_load)


async def check_alarm(units, metric_func, stat_func, threshold, comparator):
    metrics = await asyncio.gather(*[
        metric_func(unit) for unit in units])
    # filter out None vals
    metrics = [m for m in metrics if m]
    return compare(stat_func(metrics), threshold, comparator)


def compare(metric, threshold, comparator):
    if comparator == '<':
        return metric < threshold
    if comparator == '>':
        return metric > threshold
    if comparator == '==':
        return metric == threshold
    if comparator == '!=':
        return metric != threshold
    if comparator == '>=':
        return metric >= threshold
    if comparator == '<=':
        return metric <= threshold


def average(metrics):
    if not metrics:
        return 0
    return sum(metrics) / len(metrics)


class ScalingPolicy:
    """Performs a scaling action in response to an Alarm.

    Example::

        decrease-capacity:
            when: decrease-capacity-alarm
            scaling-adjustment: 1
            adjustment-type: units

    """
    def __init__(self, name, config, scaler):
        self.name = name
        self.config = config
        self.scaler = scaler
        self._warmup = None

    def scaling_adjustment(self):
        val = self.config.get('scaling-adjustment')
        return float(val) if val else None

    def adjustment_type(self):
        return self.config.get('adjustment-type')

    def warming_up(self):
        """Return True if added units are still in the warmup period.

        """
        return (
            self._warmup and
            (datetime.datetime.utcnow() - self._warmup).total_seconds() <
            self.warmup()
        )

    def warmup(self):
        """Return the warmup period in seconds.

        """
        return self.config.get('warmup', 0)

    async def add_units(self, num_units):
        if self.warming_up():
            log.debug('Not adding units, still in warmup period.')
            return

        res = await self.scaler.add_units(num_units)
        if res and self.warmup():
            self._warmup = datetime.datetime.utcnow()
        return res

    async def apply(self):
        scaling_adjustment = self.scaling_adjustment()
        if not scaling_adjustment:
            return

        if scaling_adjustment > 0:
            func = self.add_units
        else:
            func = self.scaler.destroy_units

        scaling_adjustment = abs(scaling_adjustment)
        if self.adjustment_type() == 'percent':
            current_units = len(self.scaler.app.units)
            num_units = (
                max(round((scaling_adjustment / 100.) * current_units), 1)
            )
        else:
            num_units = scaling_adjustment

        log.debug('Applying scaling policy: %s', self.name)
        return await func(int(num_units))


class Alarm:
    """Monitors an application and notifies when a condition is met.

    Example::

        add-capacity-alarm:
            statistic: average
            metric: cpu
            comparator: >=
            threshold: 80
            period: 300

    """
    def __init__(self, name, config, scaler):
        self.name = name
        self.config = config
        self.scaler = scaler
        self._task = None
        self._policies = []

        for policy, policy_cfg in self.config.get('policies', {}).items():
            self._policies.append(
                ScalingPolicy(policy, policy_cfg, self.scaler))

    def check_interval(self):
        """Return number of seconds between each condition check.

        """
        return 30

    def enable(self):
        """Start monitoring for the alarm condition.

        """
        if not self._task:
            log.debug('Enabling alarm: %s', self.name)
            self._task = self.scaler.loop.create_task(self._start_task())
        return self._task

    def disable(self):
        """Stop monitoring for the alarm condition.

        """
        if self._task:
            log.debug('Disabling alarm: %s', self.name)
            self._task.cancel()
        self._task = None

    async def _start_task(self):
        try:
            while True:
                if await self.check_alarm():
                    await self.apply_policies()

                await asyncio.sleep(self.check_interval())
        except CancelledError:
            pass

    async def check_alarm(self):
        if not (self.scaler.app and self.scaler.app.units):
            log.debug('Skipping alarm check: %s - no units present', self.name)
            return

        log.debug('Checking alarm: %s', self.name)

        return await check_alarm(
            self.scaler.app.units,
            cpu_metric,
            average,
            self.threshold(),
            self.comparator(),
        )

    def threshold(self):
        return self.config.get('threshold')

    def comparator(self):
        return self.config.get('comparator')

    async def apply_policies(self):
        if self._policies:
            await asyncio.gather(*[
                p.apply() for p in self._policies])


class AutoScaler(ModelObserver):
    """Auto-scales a single Juju application in a single model.

    """
    def __init__(self, app_name, config, model):
        """Initialize the AutoScaler.

        :param app_name: The Juju application name
        :param config: Dictionary of auto-scaling rules
        :param model: The :class:`juju.model.Model` in which to operate

        """
        self.app_name = app_name
        self.config = config
        self.model = model
        self.loop = model.loop
        self._alarms = []
        self._destroyed_units = []
        self._scale_lock = asyncio.Lock()
        self._change_lock = asyncio.Lock()

        for alarm, alarm_cfg in self.config.get('alarms', {}).items():
            self._alarms.append(Alarm(alarm, alarm_cfg, self))

        self.model.add_observer(self)

    @property
    def app(self):
        return self.model.applications.get(self.app_name)

    def max_units(self):
        return int(self.config.get('max-units', sys.maxsize))

    def min_units(self):
        return int(self.config.get('min-units', 0))

    async def on_change(self, delta, old, new, model):
        """React to changes in the model.

        """
        app = self.app

        with await self._change_lock:
            # Too few units?
            if len(app.units) < self.min_units():
                log.debug('Too few units for %s', self.app_name)
                await self.add_units(
                    self.min_units() - len(app.units)
                )

    def start(self):
        """Start the AutoScaler.

        Initiates a loop which monitors the status of the application and
        applies auto-scaling rules as appropriate.

        Under normal circumstances this will run forever, or until stopped.

        """
        log.debug('Starting auto-scaler for %s', self.app_name)
        for alarm in self._alarms:
            alarm.enable()

        # run on_change handler once at startup in case we don't get any
        # changes from the model for a while
        self.loop.create_task(
            self.on_change(None, None, None, self.model))

    async def deploy(self):
        """Deploy the application.

        Called when the AutoScaler is run against a model in which the
        application is not yet deployed.

        Requires that the AutoScaler config includes a 'deploy' block,
        otherwise an Exception will be raised.

        :return: :class:`juju.application.Application` instance

        """
        if 'deploy' not in self.config:
            log.warn(
                "%s can not be auto-deployed due to missing"
                "'deploy' block in config.", self.app_name
            )
            return

        deploy_cfg = self.config['deploy']
        charm = deploy_cfg.pop('charm', self.app_name)
        deploy_cfg.pop('application_name', None)

        log.info('Deploying %s', self.app_name)

        return await self.model.deploy(
            charm,
            application_name=self.app_name,
            **deploy_cfg,
        )

    async def destroy_units(self, num_units):
        """Destroy one or more units of the application.

        :param num_units: Number of units to destroy

        """
        with await self._scale_lock:
            if not self.app:
                return

            max_destroyable = len(self.app.units) - self.min_units()
            if max_destroyable <= 0:
                return

            num_units = min(num_units, max_destroyable)

            # func to sort by unit number so we can kill newest units first
            def cmp(name):
                return int(name.split('/')[-1])

            unit_names = [
                u.name for u in self.app.units
                if u.name not in self._destroyed_units
            ]
            unit_names = sorted(unit_names, key=cmp, reverse=True)

            self._destroyed_units.extend(unit_names[:num_units])
            return await self.model.destroy_units(*unit_names[:num_units])

    async def add_units(self, num_units):
        """Add one or more units of the application.

        :param num_units: Number of units to add

        """
        with await self._scale_lock:
            if not self.app:
                return

            max_addable = self.max_units() - len(self.app.units)
            if max_addable <= 0:
                return

            num_units = min(num_units, max_addable)

            return await self.app.add_units(count=num_units)


async def run(loop, args):
    scalers = []

    model = Model(loop)
    await model.connect_current()

    config = yaml.load(args.config)
    for app, app_config in config.items():
        scalers.append(AutoScaler(app, app_config, model))

    for scaler in scalers:
        scaler.start()


def main():
    parser = setup_parser()
    args = parser.parse_args()

    logging.basicConfig(
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
        level=getattr(logging, args.log_level),
    )
    ws_logger = logging.getLogger('websockets.protocol')
    ws_logger.setLevel(logging.INFO)

    loop = asyncio.get_event_loop()
    for signame in ('SIGINT', 'SIGTERM'):
        loop.add_signal_handler(getattr(signal, signame), loop.stop)

    try:
        loop.create_task(run(loop, args))
        loop.run_forever()
    finally:
        loop.close()


if __name__ == '__main__':
    main()