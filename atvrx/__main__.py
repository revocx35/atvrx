import logging
import os

import uvicorn

from .web import create_app, trusted_proxies

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("ATVRX_HTTP_PORT", "8095")), log_level="info",
            proxy_headers=True, forwarded_allow_ips=trusted_proxies())
