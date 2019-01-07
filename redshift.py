#!/usr/bin/env python3

import argparse
from bisect import bisect
from datetime import datetime
import json
import logging
import sys
import time

import paho.mqtt.client as mqtt



class RedshiftCalculator(object):

    @classmethod
    def _interpolate(cls, x0, x, y):
        # linear interpolation
        i = bisect(x, x0, 1, len(x)-1)
        m = (x0 - x[i-1]) / (x[i] - x[i-1])
        return m*y[i] + (1.-m)*y[i-1]

    def __init__(self, time, colortemp, brightness):
        # times need to be in ascending order
        assert min(time) >= 0
        assert max(time) <= 24
        assert all( (time[i-1] < time[i]) for i in range(1, len(time)) )

        # add "virtual" first/last hour so that interpolation correctly wraps around midnight
        first_time, last_time = time[0], time[-1]
        first_ct,   last_ct   = colortemp[0],  colortemp[-1]
        first_bt,   last_bt   = brightness[0], brightness[-1]
        if time[0] > 0:
            time.insert(0, last_time-24)
            colortemp.insert(0, last_ct)
            brightness.insert(0, last_bt)
        if time[-1] < 24:
            time.append(first_time+24)
            colortemp.append(first_ct)
            brightness.append(first_bt)

        self.time       = time
        self.colortemp  = colortemp
        self.brightness = brightness

    def __call__(self):
        now  = datetime.now()
        day  = datetime(now.year, now.month, now.day)
        hour = (now-day).total_seconds() / 60. / 60.
        return { 'brightness': self._interpolate(hour, self.time, self.brightness),
                 'colortemp':  self._interpolate(hour, self.time, self.colortemp) }


# configure logging
logging.basicConfig(level=logging.INFO,
                    format='[%(asctime)s] %(levelname)-7s %(name)-12s -- %(message)s',
                    datefmt='%Y/%m/%d %H:%M:%S')
logger = logging.getLogger('REDSHIFT')

# parse command line arguments
parser = argparse.ArgumentParser()
parser.add_argument('--config', default='config.json', help='Configuration file')
args = parser.parse_args()

# load configuration
with open(args.config, 'r') as f:
    config = json.load(f)

# initialize calculator
redshift = RedshiftCalculator(config['Redshift']['Time'],
                              config['Redshift']['ColorTemp'],
                              config['Redshift']['Brightness'])

# track state of lights
lights = dict()

# set up MQTT
def on_connect(client, userdata, flags, rc):
    logger.info('Connected!')

def on_disconnect(client, userdata, rc):
    logger.info('Disconnected!')
    sys.exit(1)

def on_publish(client, userdata, mid):
    logger.debug('Message {:} published!'.format(mid))

def on_message(client, userdata, message):
    if message.payload is None:
        return

    try:
        topic = message.topic
        logger.info('Received message for {:}'.format(topic))

        # get previous and new state of the lamp
        global lights
        new_state = json.loads(message.payload)
        old_state = lights.setdefault(topic, { 'on': False, 'changed': datetime(1900,1,1) })

        # only adjust color on lights which are "on"
        was_turned_on = new_state['on'] and (not old_state['on'])
        needs_update  = (datetime.now() - old_state['changed']).total_seconds() > config['Redshift']['AdjustSeconds']
        if new_state['on'] and (was_turned_on or needs_update):
            set_state = json.dumps(redshift())
            logger.info('{:} -> {:}'.format(topic+'/set', set_state))
            client.publish(topic+'/set', set_state, 0, False)
            lights[topic]['changed'] = datetime.now()

        lights[topic]['on'] = new_state['on']
    except:
        import traceback
        traceback.print_exc()


client = mqtt.Client(config['MQTT']['Client'], clean_session=False)
client.enable_logger(logger)
client.on_connect    = on_connect
client.on_disconnect = on_disconnect
client.on_publish    = on_publish
client.on_message    = on_message
if config['MQTT']['TLS']:
    client.tls_set()
client.connect(config['MQTT']['Host'], port=int(config['MQTT']['Port']), keepalive=60)

# subscribe to lights' status changes
for lamp in config['Redshift']['Lights']:
    client.subscribe(lamp)


# now loop, but don't update lights too often
sleep_time  = config['Redshift']['SleepSeconds']
wait_cycles = config['Redshift']['UpdateCycles']

wait = wait_cycles
while True:
    time.sleep(sleep_time)

    if wait == 0:
        # every `wait_cycles` update a light that needs to be adjusted
        for path, light in lights.items():
            if (datetime.now() - light['changed']).total_seconds() > config['Redshift']['AdjustSeconds']:
                set_state = json.dumps(redshift())
                logger.info('{:} -> {:}'.format(path+'/set', set_state))
                client.publish(path+'/set', set_state, 0, False)
                light['changed'] = datetime.now()
                break
        wait = wait_cycles

    else:
        wait -= 1

    client.loop()
