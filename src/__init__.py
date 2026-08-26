"""Discount Parser application package."""

# Customer-facing compatibility patches are installed at package import time so
# web, scheduler, Telegram bot, CLI and tests all use the same offer contract.
from src.customer_feedback_14 import install_customer_feedback_14
from src.customer_feedback_14_promokood import install_promokood_block_parser
from src.customer_feedback_15 import install_customer_feedback_15
from src.customer_feedback_16 import install_customer_feedback_16
from src.modules.source_registry.follow_collection import install_follow_profile_collection
from src.modules.source_registry.image_profiles import install_profile_image_extraction

install_customer_feedback_14()
install_promokood_block_parser()
install_customer_feedback_15()
install_customer_feedback_16()

# DP-CUST-017: website collection used to install the two-stage/follow-profile
# collector only when the web UI process imported src.web.application. The
# packaged scheduler runs in DiscountParserWorker.exe and therefore collected
# sites with the unpatched GenericWebCollector while Telegram still worked.
# Install both website extraction layers here so UI, worker, bot and CLI share
# exactly the same collector behavior.
install_follow_profile_collection()
install_profile_image_extraction()
