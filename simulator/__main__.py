"""Run the simulator by hand:  python -m simulator --id thermo-1 --port 1883"""

import argparse
import time

from simulator.device import ThermostatSimulator


def main():
    parser = argparse.ArgumentParser(description="MQTT thermostat simulator")
    parser.add_argument("--id", default="thermo-1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    device = ThermostatSimulator(args.id, args.host, args.port, interval_s=args.interval)
    device.start()
    print(f"Simulator {args.id} publishing to devices/{args.id}/# on {args.host}:{args.port} (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        device.stop()


if __name__ == "__main__":
    main()
