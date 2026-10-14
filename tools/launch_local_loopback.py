#!/usr/bin/env python3
"""Two isolated desktop stations on crossed, private PulseAudio audio paths."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-root', type=Path, default=ROOT)
    parser.add_argument('--call-a', default='AG7SU')
    parser.add_argument('--call-b', default='N0TST')
    parser.add_argument('--data-dir', type=Path, default=Path.home()/'.local/share/PixelQSO/local-loopback')
    args = parser.parse_args()
    modules, children, logs = [], [], []
    tag = 'pixelqso_local_' + uuid.uuid4().hex[:8]
    def pulse(*values):
        return subprocess.check_output(['pactl', *values], text=True).strip()
    try:
        for side in ('a', 'b'):
            sink = tag + '_' + side
            modules.append(pulse('load-module', 'module-null-sink', 'sink_name='+sink,
                'rate=48000', 'channels=2', 'sink_properties=device.description='+sink))
            modules.append(pulse('load-module', 'module-remap-source', 'master='+sink+'.monitor',
                'source_name='+sink+'_rx', 'rate=48000', 'channels=1', 'channel_map=mono',
                'source_properties=device.description='+sink+'_rx'))
        for side, peer, call in [('a','b',args.call_a), ('b','a',args.call_b)]:
            data=args.data_dir/side
            data.mkdir(parents=True, exist_ok=True)
            if not (data/'settings.ini').exists():
                (data/'settings.ini').write_text('[station]\ncallsign='+call+'\ngrid=CN85\naudio_test_mode=true\n\n[transfer]\nmode=experimental_qpsk_5s\nexperimental_mode=experimental_qpsk_5s\nmode_explicit=true\n')
            env=os.environ.copy()
            env.update(PIXELQSO_DATA_DIR=str(data), PIXELQSO_CALLSIGN=call,
                PIXELQSO_AUDIO_TEST='1', PIXELQSO_AUDIO_ROLE='station',
                PIXELQSO_OUTPUT_DEVICE=tag+'_'+side, PIXELQSO_INPUT_DEVICE=tag+'_'+peer+'_rx',
                QT_QPA_PLATFORM='xcb', PULSE_SINK=tag+'_'+side, PULSE_SOURCE=tag+'_'+peer+'_rx')
            env.pop('PIXELQSO_ON_AIR_CONFIG',None)
            log=(data/'session.log').open('w');logs.append(log)
            code="import os; from PySide6.QtWidgets import QApplication; from PySide6.QtCore import QTimer; import app; q=QApplication([]); q.setApplicationName('PixelQSO'); q.setOrganizationName('PixelQSO'); from PySide6.QtCore import QSettings; settings=QSettings(os.environ['PIXELQSO_DATA_DIR']+'/settings.ini',QSettings.Format.IniFormat); settings.setValue('options/backend','experimental'); settings.setValue('options/show_experimental_modes',True); settings.setValue('transfer/mode','resilient_100'); settings.setValue('transfer/experimental_mode','resilient_100'); settings.sync(); w=app.MainWindow(); w.setWindowTitle('PixelQSO loopback "+side.upper()+" · "+call+"'); w.exchange_mode.setCurrentIndex(1); w.exchange_role.setCurrentIndex("+str(0 if side == "a" else 1)+"); w.move("+str(20 if side == "a" else 1160)+",40); w.show(); print('READY',w.station_call.text(),w.selected_mode(),w.input_device.currentText(),w.output_device.currentText(),flush=True); QTimer.singleShot(1500,w.start_receive); q.exec()"
            child=subprocess.Popen([sys.executable,'-c',code],cwd=args.app_root,env=env,
                stdout=log,stderr=subprocess.STDOUT)
            children.append(child)
            print(f'{side.upper()}: {call}, PID {child.pid}, settings/logs {data}',flush=True)
        print('Audio: A TX → B RX; B TX → A RX. Close both windows or press Ctrl+C here to release the session.',flush=True)
        while any(child.poll() is None for child in children):
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            if child.poll() is None: child.terminate()
        for child in children:
            try: child.wait(timeout=5)
            except subprocess.TimeoutExpired: child.kill();child.wait()
        for log in logs:log.close()
        for module in reversed(modules):
            subprocess.run(['pactl','unload-module',module],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

if __name__=='__main__': main()
