"""
Copyright 2015-2016 Christian Fobel

This file is part of dmf_device_ui_plugin.

dmf_device_ui_plugin is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

dmf_control_board is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with dmf_device_ui_plugin.  If not, see <http://www.gnu.org/licenses/>.
"""
from datetime import datetime
from subprocess import Popen, CREATE_NEW_PROCESS_GROUP
import io
import json
import logging
import sys
import time

from flatland import Boolean, Form, Integer, String
from flatland_helpers import flatlandToDict
from microdrop.plugin_helpers import (AppDataController, StepOptionsController,
                                      get_plugin_info, hub_execute,
                                      hub_execute_async)
from microdrop.plugin_manager import (IPlugin, Plugin, PluginGlobals,
                                      ScheduleRequest, emit_signal, implements)
from microdrop.app_context import get_app, get_hub_uri
from path_helpers import path
from pygtkhelpers.utils import refresh_gui
from si_prefix import si_format
import gobject
import paho_mqtt_helpers as pmh
import pandas as pd
from zmq_plugin.schema import PandasJsonEncoder

logger = logging.getLogger(__name__)


PluginGlobals.push_env('microdrop.managed')


class DmfDeviceUiPlugin(AppDataController, StepOptionsController, Plugin,
                         pmh.BaseMqttReactor):
    """
    This class is automatically registered with the PluginManager.
    """
    implements(IPlugin)
    version = get_plugin_info(path(__file__).parent).version
    plugin_name = get_plugin_info(path(__file__).parent).plugin_name

    AppFields = Form.of(
        String.named('video_config').using(default='', optional=True,
                                           properties={'show_in_gui': False}),
        String.named('surface_alphas').using(default='', optional=True,
                                             properties={'show_in_gui':
                                                         False}),
        String.named('canvas_corners').using(default='', optional=True,
                                             properties={'show_in_gui':
                                                         False}),
        String.named('frame_corners').using(default='', optional=True,
                                            properties={'show_in_gui': False}),
        Integer.named('x').using(default=None, optional=True,
                                 properties={'show_in_gui': False}),
        Integer.named('y').using(default=None, optional=True,
                                 properties={'show_in_gui': False}),
        Integer.named('width').using(default=400, optional=True,
                                     properties={'show_in_gui': False}),
        Integer.named('height').using(default=500, optional=True,
                                      properties={'show_in_gui': False}))

    StepFields = Form.of(Boolean.named('video_enabled')
                         .using(default=True, optional=True,
                                properties={'title': 'Video'}))

    def __init__(self):
        self.name = self.plugin_name
        self.gui_process = None
        self.gui_heartbeat_id = None
        self._gui_enabled = False
        self.alive_timestamp = None
        self.should_terminate = False
        pmh.BaseMqttReactor.__init__(self)
        self.start()

    def reset_gui(self):
        py_exe = sys.executable
        # Set allocation based on saved app values (i.e., remember window size
        # and position from last run).
        app_values = self.get_app_values()
        allocation_args = ['-a', json.dumps(app_values)]

        app = get_app()
        if app.config.data.get('advanced_ui', False):
            debug_args = ['-d']
        else:
            debug_args = []

        self.gui_process = Popen([py_exe, '-m',
                                  'dmf_device_ui.bin.device_view', '-n',
                                  self.name] + allocation_args + debug_args +
                                 ['fixed', get_hub_uri()],
                                 creationflags=CREATE_NEW_PROCESS_GROUP)
        self.gui_process.daemon = False
        self._gui_enabled = True

        def keep_alive():
            if not self._gui_enabled:
                self.alive_timestamp = None
                return False
            elif self.gui_process.poll() == 0:
                # GUI process has exited.  Restart.
                self.cleanup()
                self.reset_gui()
                return False
            else:
                self.alive_timestamp = datetime.now()
                # Keep checking.
                return True
        # Go back to Undo 613 for working corners
        self.step_video_settings = None
        # Get current video settings from UI.
        app_values = self.get_app_values()
        # Convert JSON settings to 0MQ plugin API Python types.
        ui_settings = self.json_settings_as_python(app_values)

        self.set_ui_settings(ui_settings, default_corners=True)
        self.gui_heartbeat_id = gobject.timeout_add(1000, keep_alive)

    def cleanup(self):
        if self.gui_heartbeat_id is not None:
            gobject.source_remove(self.gui_heartbeat_id)

        self.alive_timestamp = None

    def get_schedule_requests(self, function_name):
        """
        Returns a list of scheduling requests (i.e., ScheduleRequest instances)
        for the function specified by function_name.
        """
        if function_name == 'on_plugin_enable':
            return [ScheduleRequest('droplet_planning_plugin', self.name)]
        elif function_name == 'on_dmf_device_swapped':
            # XXX Schedule `on_app_exit` handling before `device_info_plugin`,
            # since `hub_execute` uses the `device_info_plugin` service to
            # submit commands to through the 0MQ plugin hub.
            return [ScheduleRequest('microdrop.device_info_plugin',self.name)]
        elif function_name == 'on_app_exit':
            # XXX Schedule `on_app_exit` handling before `device_info_plugin`,
            # since `hub_execute` uses the `device_info_plugin` service to
            # submit commands to through the 0MQ plugin hub.
            return [ScheduleRequest(self.name, 'microdrop.device_info_plugin')]
        return []

    def on_app_exit(self):
        self.should_terminate = True
        self.mqtt_client.publish('microdrop/dmf-device-ui-plugin/get-video-settings',
                                 json.dumps(None))

    def json_settings_as_python(self, json_settings):
        '''
        Convert DMF device UI plugin settings from json format to Python types.

        Python types are expected by DMF device UI plugin 0MQ command API.

        Args
        ----

            json_settings (dict) : DMF device UI plugin settings in
                JSON-compatible format (i.e., only basic Python data types).

        Returns
        -------

            (dict) : DMF device UI plugin settings in Python types expected by
                DMF device UI plugin 0MQ commands.
        '''
        py_settings = {}

        corners = dict([(k, json_settings.get(k))
                        for k in ('canvas_corners', 'frame_corners')])

        if all(corners.values()):
            # Convert CSV corners lists for canvas and frame to
            # `pandas.DataFrame` instances
            for k, v in corners.iteritems():
                # Prepend `'df_'` to key to indicate the type as a data frame.
                py_settings['df_' + k] = pd.read_csv(io.BytesIO(bytes(v)),
                                                     index_col=0)

        for k in ('video_config', 'surface_alphas'):
            if k in json_settings:
                if not json_settings[k]:
                    py_settings[k] = pd.Series(None)
                else:
                    py_settings[k] = pd.Series(json.loads(json_settings[k]))

        return py_settings

    def save_ui_settings(self, video_settings):
        '''
        Save specified DMF device UI 0MQ plugin settings to persistent
        Microdrop configuration (i.e., settings to be applied when Microdrop is
        launched).

        Args
        ----

            video_settings (dict) : DMF device UI plugin settings in
                JSON-compatible format returned by `get_ui_json_settings`
                method (i.e., only basic Python data types).
        '''
        app_values = self.get_app_values()
        # Select subset of app values that are present in `video_settings`.
        app_video_values = dict([(k, v) for k, v in app_values.iteritems()
                                 if k in video_settings.keys()])

        # If the specified video settings differ from app values, update
        # app values.
        if app_video_values != video_settings:
            app_values.update(video_settings)
            self.set_app_values(app_values)

    def set_ui_settings(self, ui_settings, default_corners=False):
        '''
        Set DMF device UI settings from settings dictionary.

        Args
        ----

            ui_settings (dict) : DMF device UI plugin settings in format
                returned by `json_settings_as_python` method.
        '''

        if 'video_config' in ui_settings:
            msg = {}
            msg['video_config'] = ui_settings['video_config'].to_json()
            self.mqtt_client.publish('microdrop/dmf-device-ui-plugin/set-video-config',
                                      payload=json.dumps(msg), retain=True)

        if 'surface_alphas' in ui_settings:
            # TODO: Make Clear retained messages after exit
            msg = {}
            msg['surface_alphas'] = ui_settings['surface_alphas'].to_json()
            self.mqtt_client.publish('microdrop/dmf-device-ui-plugin/set-surface-alphas',
                                      payload=json.dumps(msg), retain=True)

        if all((k in ui_settings) for k in ('df_canvas_corners',
                                            'df_frame_corners')):
            # TODO: Test With Camera
            msg = {}
            msg['df_canvas_corners'] = ui_settings['df_canvas_corners'].to_json()
            msg['df_frame_corners']  = ui_settings['df_frame_corners'].to_json()

            if default_corners:
                self.mqtt_client.publish('microdrop/dmf-device-ui-plugin/'
                                          'set-default-corners',
                                          payload=json.dumps(msg), retain=True)
            else:
                self.mqtt_client.publish('microdrop/dmf-device-ui-plugin/'
                                          'set-corners',
                                          payload=json.dumps(msg), retain=True)

    # #########################################################################
    # # Plugin signal handlers
    def on_connect(self, client, userdata, flags, rc):
        self.mqtt_client.subscribe('microdrop/dmf-device-ui/get-video-settings')
        self.mqtt_client.subscribe('microdrop/dmf-device-ui/update-protocol')

    def on_message(self, client, userdata, msg):
        if msg.topic == 'microdrop/dmf-device-ui/get-video-settings':
            self.video_settings = json.loads(msg.payload)
            self.save_ui_settings(self.video_settings)
            if self.should_terminate:
                self.mqtt_client.publish('microdrop/dmf-device-ui-plugin/terminate')
        if msg.topic == 'microdrop/dmf-device-ui/update-protocol':
            self.update_protocol(json.loads(msg.payload))

    def on_plugin_disable(self):
        self._gui_enabled = False
        self.cleanup()


    def on_plugin_enable(self):
        super(DmfDeviceUiPlugin, self).on_plugin_enable()
        self.reset_gui()

        form = flatlandToDict(self.StepFields)
        self.mqtt_client.publish('microdrop/dmf-device-ui-plugin/schema',
                                  json.dumps(form),
                                  retain=True)
        defaults = {}
        for k,v in form.iteritems():
            defaults[k] = v['default']

        # defaults = map(lambda (k,v): {k: v['default']}, form.iteritems())
        self.mqtt_client.publish('microdrop/dmf-device-ui-plugin/step-options',
                                  json.dumps([defaults], cls=PandasJsonEncoder),
                                  retain=True)

    def on_step_removed(self, step_number, step):
        self.update_steps()

    def on_step_options_changed(self, plugin, step_number):
        self.update_steps()

    def on_step_run(self):
        '''
        Handler called whenever a step is executed.

        Plugins that handle this signal must emit the on_step_complete signal
        once they have completed the step. The protocol controller will wait
        until all plugins have completed the current step before proceeding.
        '''
        app = get_app()
        # TODO: Migrate video commands to mqtt!!
        # if (app.realtime_mode or app.running) and self.gui_process is not None:
        #     step_options = self.get_step_options()
        #     if not step_options['video_enabled']:
        #         hub_execute(self.name, 'disable_video',
        #                     wait_func=lambda *args: refresh_gui(), timeout_s=5,
        #                     silent=True)
        #     else:
        #         hub_execute(self.name, 'enable_video',
        #                     wait_func=lambda *args: refresh_gui(), timeout_s=5,
        #                     silent=True)
        emit_signal('on_step_complete', [self.name, None])

    def update_steps(self):
        app = get_app()
        num_steps = len(app.protocol.steps)

        protocol = []
        for i in range(num_steps):
            protocol.append(self.get_step_options(i))

        self.mqtt_client.publish('microdrop/dmf-device-ui-plugin/step-options',
                                  json.dumps(protocol, cls=PandasJsonEncoder),
                                  retain=True)

    def update_protocol(self, protocol):
        app = get_app()

        for i, s in enumerate(protocol):

            step = app.protocol.steps[i]
            prevData = step.get_data(self.plugin_name)
            values = {}

            for k,v in prevData.iteritems():
                if k in s:
                    values[k] = s[k]

            step.set_data(self.plugin_name, values)
            emit_signal('on_step_options_changed', [self.plugin_name, i],
                        interface=IPlugin)

PluginGlobals.pop_env()

from ._version import get_versions
__version__ = get_versions()['version']
del get_versions
