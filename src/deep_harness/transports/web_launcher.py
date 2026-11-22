"""Launch the browser workbench with one owning Uvicorn worker."""

import uvicorn

from deep_harness.application.instance import InstanceHost
from deep_harness.application.instance_config import load_instance_config
from deep_harness.server_config import setting
from deep_harness.transports.web import STATIC, create_web_api


def serve_web(args):
    config_path = args.instance_config.resolve()
    settings = load_instance_config(config_path)
    assets = (args.assets or STATIC).resolve()
    if not (assets / "index.html").is_file():
        raise SystemExit("Web assets are missing. Run npm run build from desktop first.")
    instance = InstanceHost(settings, args.state_dir, config_path=config_path)
    uvicorn.run(
        create_web_api(instance, assets),
        host=settings.web.host,
        port=settings.web.port,
        workers=1,
        proxy_headers=False,
        log_level=setting("transport.cli_log_level"),
    )
