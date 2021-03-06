#!/usr/bin/env python

from __future__ import division

from monitors.screen.screen import Screen
from services.iterationdata import IterationData
from services.usagedata import UsageData
from utils.foregrounddetector import ForegroundDetector
from utils.hardware import Hardware

import os
import random
import struct


class OLED(Screen):

    NSAMPLES = 500

    def __init__(self, devconstants):
        super(OLED, self).__init__(Hardware.OLED, devconstants)

        self._fb_file = None
        self._fb_samples = []
        self._setup_fb()
        self._setup_fb_samples()

        # 65025 = 255^2
        self.RED_PWR = devconstants.OLED_CHANNEL_PWRS[0] / 65025
        self.GREEN_PWR = devconstants.OLED_CHANNEL_PWRS[1] / 65025
        self.BLUE_PWR = devconstants.OLED_CHANNEL_PWRS[2] / 65025
        # 585225 = 65025 * 3^2 (three colors)
        self.MODULATION_PWR = devconstants.OLED_MODULATION_PWR / 585225

    def _setup_fb(self):
        """ Choose framebuffer file
        """
        # TODO: Change permission to read file
        if os.path.isfile("/dev/fb0"):
            self._fb_file = "/dev/fb0"
        elif os.path.isfile("/dev/graphics/fb0"):
            self._fb_file = "/dev/graphics/fb0"

    def _setup_fb_samples(self):
        """ Choose samples from framebuffer for averaging pixel color
        impact on power usage. """

        factor = self.width * self.height // self.NSAMPLES
        for i in xrange(self.NSAMPLES):
            self._fb_samples.append((factor * i) + random.randint(0, factor))

    def calc_iteration(self, iter_num):
        """ Return power usage of each application using display after one
        iteration. """
        result = IterationData()

        brightness = Screen.get_display_brightness()

        if 0 <= brightness <= 255:
            self.logger.warn("Could not retrieve brightness information")
            return result

        with self._screenlock:
            screen = self.screen_on

        px_pwr = 0.0

        # TODO: Substitute with C-based getScreenPixPower native function
        if screen and self._fb_file is not None:
            try:
                with open(self._fb_file) as fp:
                    for x in self._fb_samples:
                        fp.seek(x * 4)
                        # Read 32-bit integer from file
                        px = struct.unpack('i', fp.read(4))[0]
                        blue = px >> 8 & 0xFF
                        green = px >> 16 & 0xFF
                        red = px >> 24 & 0xFF

                        # Calculate the power usage of this pixel as if it were
                        # at full brightness. Linearly scale by brightness to
                        # get true power usage. To calculate whole screen power
                        # usage, compute average of sampled region and multiply
                        # by number of pixels

                        sum_colors = red + green + blue
                        px_pwr += (self.RED_PWR * (red*red) +
                                   self.GREEN_PWR * (green*green) +
                                   self.BLUE_PWR * (blue*blue) -
                                   self.MODULATION_PWR *
                                   (sum_colors * sum_colors))
            except IOError as (errno, strerr):
                self.logger.warn("Can't read framebuffer {0}".format(strerr))
                px_pwr = 0.0

            if px_pwr > 0.0:
                px_pwr *= self.width * self.height / self.NSAMPLES

        if screen:
            usage = OLEDUsage(brightness, px_pwr)
            uid = ForegroundDetector.get_foreground_uid()
            result.set_uid_usage(uid, usage)
        else:
            usage = OLEDUsage()

        result.set_sys_usage(usage)

        return result


class OLEDUsage(UsageData):

    __slots__ = ['screen_on', 'brightness', 'pix_pwr']

    def __init__(self, screen_on=False, brightness=0, pix_pwr=0.0):
        super(OLEDUsage, self).__init__()
        self.screen_on = screen_on
        self.brightness = brightness
        self.pix_pwr = pix_pwr

    def log(self, out):
        res = "OLED-brightness {} OLED-screen-on {} OLED-pix_power {}\n"
        out.write(res.format(self.brightness, self.screen_on, self.pix_pwr))
