"""SMS data pipe.

- ``send_sms``  — outbound, called from the CLI and the adapter.
- ``server``    — FastAPI inbound webhook (mounts ``app``).
- ``cli``       — argparse CLI for one-shot sends.
"""

from sms._client import send_sms

__all__ = ["send_sms"]
