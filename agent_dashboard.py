"""Start the local AIDD task dashboard without additional dependencies."""
import argparse

from agents.harness.dashboard import Dashboard, make_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--task-root", default="runs")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()
    server = make_server(Dashboard(args.task_root, args.config), args.port)
    print(f"AIDD 控制台：http://127.0.0.1:{server.server_port}", flush=True)
    print("关闭控制台不会取消已启动任务；请先在页面中暂停或取消。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
