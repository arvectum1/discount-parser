"""Discount Parser application package."""

# DP-CUST-014 is installed at package import time so every runtime entrypoint
# (web, scheduler, Telegram bot, CLI and tests) uses the same offer contract.
from src.customer_feedback_14 import install_customer_feedback_14
from src.customer_feedback_14_promokood import install_promokood_block_parser

install_customer_feedback_14()
install_promokood_block_parser()
