"""Discount Parser application package."""

# Customer-facing compatibility patches are installed at package import time so
# web, scheduler, Telegram bot, CLI and tests all use the same offer contract.
from src.customer_feedback_14 import install_customer_feedback_14
from src.customer_feedback_14_promokood import install_promokood_block_parser
from src.customer_feedback_15 import install_customer_feedback_15
from src.customer_feedback_16 import install_customer_feedback_16

install_customer_feedback_14()
install_promokood_block_parser()
install_customer_feedback_15()
install_customer_feedback_16()
