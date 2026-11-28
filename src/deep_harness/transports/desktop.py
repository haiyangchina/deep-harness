"""Desktop-owned loopback API; its capability arrives through a private stdin pipe."""

import asyncio
import json
import socket
import sys

import uvicorn

from deep_harness.cli import build_host, parser
from deep_harness.server_config import number, setting
from deep_harness.transports.api import create_api


async def run(args, token):
    host = build_host(args)
    try:
        # Acquire the profile owner before reporting readiness to the native shell.
        await host.application(args.profile)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(number("transport.desktop_socket_backlog"))
            listener.setblocking(False)

            class DesktopServer(uvicorn.Server):
                async def startup(self, sockets=None):
                    await super().startup(sockets=sockets)
                    if self.started:
                        print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
                        self.shutdown_reader = asyncio.create_task(self.watch_parent())

                async def watch_parent(self):
                    # A shutdown line OR parent pipe EOF stops this owned backend.
                    await asyncio.to_thread(sys.stdin.readline)
                    self.should_exit = True

            config = uvicorn.Config(
                create_api(host, token, workspace_id=args.profile),
                log_level=setting("transport.desktop_log_level"),
            )
            server = DesktopServer(config)
            await server.serve(sockets=[listener])
    finally:
        await host.close()


def main():
    args = parser().parse_args()
    if args.command != "serve":
        raise SystemExit("Desktop transport requires serve")
    request = json.loads(sys.stdin.readline())
    token = request["token"]
    if not isinstance(token, str) or len(token) < number("transport.min_operator_token_chars"):
        raise SystemExit("Invalid desktop capability")
    asyncio.run(run(args, token))


if __name__ == "__main__":
    main()
