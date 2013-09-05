#!/usr/bin/env python
#

from __future__ import division
try:
    from libs.telephony import TelephonyAccess
except ImportError:
    from utils.androidhelpers.telephony import TelephonyAccess
from monitors.telephony import Telephony
from monitors.powermonitor import PowerMonitor
from services.usagedata import UsageData
from services.powerestimator import PowerEstimator
from utils.hardware import Hardware
from utils.sysfs import Node
from utils.systeminfo import SystemInfo

import os
import time

class ThreeG(PowerMonitor):
    POWER_STATE_IDLE = 0
    POWER_STATE_FACH = 1
    POWER_STATE_HIGH = 2

    NET_STATISTIC_MASK = "/sys/devices/virtual/net/{0}/statistics"
    RX_PKT_MASK = "/sys/devices/virtual/net/{0}/statistics/rx_packets"
    TX_PKT_MASK = "/sys/devices/virtual/net/{0}/statistics/tx_packets"
    RX_BYTE_MASK = "/sys/devices/virtual/net/{0}/statistics/rx_bytes"
    TX_BYTE_MASK = "/sys/devices/virtual/net/{0}/statistics/tx_bytes"
    UID_STATS_FOLDER = "/proc/uid_state/"
    UID_TX_BYTE_MASK = UID_STATS_FOLDER.join("{0}/tcp_snd")
    UID_RX_BYTE_MASK = UID_STATS_FOLDER.join("{0}/tcp_rcv")

    def __init__(self, devconstants, iface="rmnet0"):
        super(ThreeG, self).__init__(Hardware.THREEG, devconstants)
        self._telephony = TelephonyAccess()
        self.iface = devconstants.THREEG_INTERFACE
        self._provider = self._telephony.get_operator_name()
        self._state = ThreeGState(
                devconstants.get_3g_dhcfach_time(self._provider),
                devconstants.get_3g_fachidle_time(self._provider),
                devconstants.get_3g_tx_queue(self._provider),
                devconstants.get_3g_rx_queue(self._provider))

        self._uid_states = {}
        self._sysfs = Node(self.NET_STATISTICS_MASK.format(self.iface))

        # Test file existence
        self.has_uid_information = os.access(self.UID_STATS_FOLDER, os.F_OK)

    def calc_iteration(self, iter_num):
        """ Return power usage of each application using 3G interface during
        one iteration."""
        result = IterationData()

        net_type = self._telephony.get_network_type()

        # Seems like TelephonyManager.NETWORK_TYPE_HSDPA = 8
        # TODO: Actually get models for the different network types
        if (net_type != TelephonyAccess.NETWORK_TYPE_UMTS) and (net_type !=
                TelephonyAccess.NETWORK_TYPE_HSDPA):
                net_type = TelephonyAccess.NETWORK_TYPE_UMTS

        net_state = self._telephony.get_state()
        if (net_state != TelephonyAccess.DATA_CONNECTED) or (net_type !=
                TelephonyAccess.NETWORK_TYPE_UMTS and net_type !=
                TelephonyAccess.NETWORK_TYPE_HSDPA):
            # We need to allow the real interface state to reset itself so that
            # the next update it knows it's coming back from an off state. We
            # also need to clear all UID information
            self._provider = None
            self._state.interface_off()
            self._uid_states.clear()

            result.set_sys_usage(ThreeGUsage())

            return result

        tx_pkts = int(self._sysfs.tx_packets)
        rx_pkts = int(self._sysfs.rx_packets)
        tx_bytes = int(self._sysfs.tx_bytes)
        rx_bytes = int(self._sysfs.rx_bytes)

        if (tx_bytes == -1) or (rx_bytes == -1):
            self.logger.warn("Failed to read  UID Tx/Rx byte counts")
            return result
        self._state.update(tx_pkts, rx_pkts, tx_bytes, rx_bytes)

        if self._state.is_initialized():
            result.set_sys_usage(ThreeGUsage(self._state.delta_pkts,
                self._state.tx_bytes, self._state.rx_bytes,
                self._state.pwr_state, self._provider))

        uids = SystemInfo.get_uids()

        if uids is not None:
            for uid in uids:
                if uid < 0:
                    break

                uid_state = self._uid_states.setdefault(uid, ThreeGState())

                if uid_state.is_stale():
                    # Use heuristic to not poll for UIDs that haven't had much
                    # activity recently
                    continue

                # Read operations are the expensive part of polling
                tx_bytes = Node(self.UID_TX_BYTE_MASK.format(uid))
                rx_bytes = Node(self.UID_RX_BYTE_MASK.format(uid))

                if (rx_bytes == -1) or (tx_bytes == -1):
                    self.logger.warn("Failed to read UID Tx/Rx byte counts")
                elif uid_state.is_initialized():
                    uid_state.update(0, 0, tx_bytes, rx_bytes)

                    if ((uid_state.tx_bytes + uid_state.rx_bytes != 0) or
                            (uid_state.pwr_state != self.POWER_STATE_IDLE)):
                        usage = ThreeGUsage(uid_state.delta_pkts,
                                uid_state.tx_bytes, uid_state.rx_bytes,
                                uid_state.pwr_state, self._provider)
                        result.set_uid_usage(uid, usage)
                else:
                    uid_state.update(0, 0, tx_bytes, rx_bytes)

        return result

class ThreeGUsage(UsageData):

    __slots__ = ['pwr_state', 'provider']

    def __init__(self, pkts, tx_bytes, rx_bytes, pwr_state, provider):
        super(ThreeGUsage, self).__init__()

        self.pkts = pkts
        self.tx_bytes = tx_bytes
        self.rx_bytes = rx_bytes
        self.pwr_state = pwr_state
        self.provider = provider

    def log(out):
        res = "3G-on {0}\n3G-tx_bytes {1}\n3G-rx_bytes {2}\n3G-pwr_state {3}\n3G-providerr {4}\n".format(
                self.pkts, self.tx_bytes, self.rx_bytes, self.pwr_state,
                self.provider)
        out.write(res)

class ThreeGState(object):

    __slots__ = ['pwr_state', '_dch_fach_time', 'fach_idle_time',
            '_txqueue_size', '_rxqueue_size']

    def __init__(self, dch_fach_time, fach_idle_time, tx_queue_size,
            rx_queue_size):
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.delta_pkts = 0
        self.delta_tx_bytes = 0
        self.delta_rx_bytes = 0
        self.pwr_state = ThreeG.POWER_STATE_IDLE
        self._pwr_state_time = 0
        self._inactive_time = 0
        self._update_time = None
        self._dch_fach_time = dch_fach_time
        self._fach_idle_time = fach_idle_time
        self._txqueue_size = txqueue_size
        self._rxqueue_size = rxqueue_size

    def interface_off(self):
        self._update_time = round(time.time())
        self.pwr_state = self.POWER_STATE_IDLE

    def is_initialized(self):
        return self._update_time is not None

    def update(self, tx_pkts, rx_pkts, tx_bytes, rx_bytes):
        now = round(time.time())

        if (self._update_time is not None) and (now > self._update_time):
            delta_time = now - self._update_time
            self.delta_pkts = tx_pkts + rx_pkts - self.tx_pkts - self.rx_pkts
            self.delta_tx_bytes = tx_bytes - self.tx_bytes
            self.delta_rx_bytes = rx_bytes - self.rx_bytes

            inactive = (delta_tx_bytes == 0) and (delta_rx_bytes == 0)
            if inactive:
                self._inactive_time += delta_time
            else:
                self._inactive_time = 0

            # TODO: Make this always work
            time_mult = 1
            if PowerEstimator.ITERATION_INTERVAL % 1000 != 0:
                self.logger.warn("Cannot handle 1-sec interation intervals")
            else:
                time_mult = 1000 // PowerEstimator.ITERATION_INTERVAL

            if self.pwr_state == ThreeG.POWER_STATE_IDLE and not inactive:
                self.pwr_state = ThreeG.POWER_STATE_FACH
            elif self.pwr_state == ThreeG.POWER_STATE_FACH:
                if inactive:
                    self._pwr_state_time += 1
                    if (self._pwr_state_time >=
                            (self._fach_idle_time * time_mult)):
                        self._pwr_state_time = 0
                        self.pwr_state = ThreeG.POWER_STATE_IDLE
                else:
                    self._pwr_state_time = 0
                    if (self.delta_tx_bytes > 0) or (self.delta_rx_bytes > 0):
                        self.pwr_state = ThreeG.POWER_STATE_DCH
            elif self.pwr_state == ThreeG.POWER_STATE_DCH:
                if inactive:
                    self._pwr_state_time += 1
                    if self._pwr_state_time >= self._dch_fach_time * time_mult:
                        self._pwr_state_time = 0
                        self.pwr_state = ThreeG.POWER_STATE_FACH
                else:
                    self._pwr_state_time = 0

        self._update_time = now
        self.tx_pkts = tx_pkts
        self.rx_pkts = rx_pkts
        self.tx_bytes = tx_bytes
        self.rx_bytes = rx_bytes

    def is_stale(self):
        """ Heuristic to avoid excessive polling on UIDs. We shouldn't
        update state on every iteration as it takes too much time

        None -> Boolean
        """
        if self.pwr_state != ThreeG.POWER_STATE_IDLE:
            return True

        # TODO: check if 10000 us the correct number (Why 10s?)
        return (round(time.time()) - self._update_time) > min(10000,
                self._inactive_time)