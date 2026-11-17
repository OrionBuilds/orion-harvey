"""Runs the SMS channel's drain each cycle, after replies were read (like LinkedIn)."""

import logging

from harvey.channels.drain import drain_channel
from harvey.channels.sms import SmsChannel

logger = logging.getLogger("harvey.sms.agent")


class SmsAgent:
    def __init__(self, brain, state, config, env):
        self.state, self.config, self.env = state, config, env

    @property
    def active(self) -> bool:
        return bool(self.config.channels.sms.enabled) and SmsChannel(self.config, self.env).is_configured()

    async def run(self):
        if not self.active:
            return 0
        return await drain_channel(self.state, SmsChannel(self.config, self.env, self.state))
