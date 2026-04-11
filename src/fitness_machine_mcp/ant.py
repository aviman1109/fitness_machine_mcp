"""ANT+ sensor reader — HR (type 120) and Bicycle Power (type 11).

Runs openant in a daemon thread; engine polls `manager.data` for fallback
values when BLE drops. Import is deferred so the MCP still starts if
openant / libusb is absent.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("fitness_machine.ant")

# ANT+ public network key (list, not bytes — openant concatenates internally)
_ANTPLUS_KEY = [0xB9, 0xA5, 0x21, 0xFB, 0xBD, 0x72, 0xC3, 0x45]

_HR_DEVICE_TYPE  = 120
_PWR_DEVICE_TYPE = 11
_RSC_DEVICE_TYPE = 124   # Running Speed & Cadence
_HR_PERIOD       = 8070  # ~4 Hz
_PWR_PERIOD      = 8182  # ~4 Hz
_RSC_PERIOD      = 8086  # ~4 Hz
_ANTPLUS_RF_FREQ = 57


def _ant_pct(raw: int) -> float:
    """Convert a 0.5%-unit ANT+ field to float, or 0.0 if unsupported (0xFF)."""
    return round(raw * 0.5, 1) if raw != 0xFF else 0.0


@dataclass
class ANTData:
    # HR (device type 120)
    hr_bpm:          int   = 0
    rr_ms:           int   = 0    # derived from page 0x04 event times (0 = not available)
    last_hr_ts:      float = 0.0   # monotonic; 0 = never received
    # Cycling Power (device type 11)
    power_w:         int   = 0
    cadence_rpm:     int   = 0
    last_pwr_ts:     float = 0.0   # monotonic; 0 = never received
    # Page 0x13 — Torque Effectiveness & Pedal Smoothness (0.0 = not reported)
    torque_eff_l:    float = 0.0
    torque_eff_r:    float = 0.0
    pedal_smooth_l:  float = 0.0
    pedal_smooth_r:  float = 0.0
    # Running Speed & Cadence (device type 124)
    cadence_spm:     int   = 0    # steps per minute (both feet)
    stride_len_m:    float = 0.0  # instantaneous stride length (m)
    active:          bool  = False


class ANTManager:
    """Open HR and power channels on the ANTUSB2 stick.

    Usage::

        mgr = ANTManager()
        if mgr.start():
            # workout loop reads mgr.data.hr_bpm / .power_w / .cadence_rpm
            ...
        mgr.stop()
    """

    def __init__(self):
        self.data = ANTData()
        self._node  = None
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Open ANT+ channels and start node thread.  Returns False on any error."""
        try:
            from openant.easy.node import Node
            from openant.easy.channel import Channel
        except ImportError:
            log.warning("openant not installed — ANT+ disabled")
            return False

        try:
            node = Node()
            node.set_network_key(0x00, _ANTPLUS_KEY)

            # Heart-rate channel (wildcard device ID)
            hr_ch = node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
            hr_ch.set_id(0, _HR_DEVICE_TYPE, 0)
            hr_ch.set_period(_HR_PERIOD)
            hr_ch.set_rf_freq(_ANTPLUS_RF_FREQ)
            hr_ch.on_broadcast_data = self._on_hr
            hr_ch.open()

            # Bicycle-power channel (wildcard device ID)
            pwr_ch = node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
            pwr_ch.set_id(0, _PWR_DEVICE_TYPE, 0)
            pwr_ch.set_period(_PWR_PERIOD)
            pwr_ch.set_rf_freq(_ANTPLUS_RF_FREQ)
            pwr_ch.on_broadcast_data = self._on_pwr
            pwr_ch.open()

            # Running Speed & Cadence channel (wildcard — HRM-Pro+ broadcasts RSC)
            rsc_ch = node.new_channel(Channel.Type.BIDIRECTIONAL_RECEIVE)
            rsc_ch.set_id(0, _RSC_DEVICE_TYPE, 0)
            rsc_ch.set_period(_RSC_PERIOD)
            rsc_ch.set_rf_freq(_ANTPLUS_RF_FREQ)
            rsc_ch.on_broadcast_data = self._on_rsc
            rsc_ch.open()

            self._node = node
            self._thread = threading.Thread(
                target=self._run_node, name="ant-node", daemon=True
            )
            self._thread.start()
            self.data.active = True
            log.info("ANT+ node started (HR + Power channels open)")
            return True

        except Exception as exc:
            log.warning("ANT+ init failed: %s", exc)
            self._node = None
            return False

    def stop(self):
        if self._node:
            try:
                self._node.stop()
            except Exception:
                pass
            self._node = None
        self.data.active = False
        log.info("ANT+ node stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_node(self):
        try:
            self._node.start()
        except Exception as exc:
            log.warning("ANT+ node exited: %s", exc)

    def _on_hr(self, data):
        """Broadcast from HR monitor.

        All pages: byte 7 = instantaneous HR.
        Page 0x04 (Previous Heart Beat): bytes 3-4 = prev event time,
          bytes 5-6 = current event time (1/1024 s units) → derive RR interval.
        """
        try:
            d = list(data)
            if len(d) < 8:
                return
            self.data.hr_bpm = d[7]
            self.data.last_hr_ts = time.monotonic()
            if d[0] == 0x04:
                prev_time = d[3] | (d[4] << 8)
                curr_time = d[5] | (d[6] << 8)
                rr_raw = (curr_time - prev_time) & 0xFFFF
                # Sanity: 200 ms (300 bpm) – 2000 ms (30 bpm)
                rr_ms = round(rr_raw * 1000 / 1024)
                if 200 <= rr_ms <= 2000:
                    self.data.rr_ms = rr_ms
        except Exception:
            pass

    def _on_pwr(self, data):
        """Broadcast from power meter.

        Page 0x10 (Standard Power):
          [0] page  [1] event cnt  [2] pedal power  [3] cadence
          [4-5] accum power LE   [6-7] instant power LE

        Page 0x13 (Torque Effectiveness & Pedal Smoothness):
          [0] page  [1] torque_eff_L  [2] torque_eff_R
          [3] smooth_L  [4] smooth_R  [5-7] reserved
          All values in 0.5% units; 0xFF = not supported.
        """
        try:
            d = list(data)
            if len(d) < 8:
                return
            page = d[0]
            if page == 0x10:
                cadence = d[3]
                power   = d[6] | (d[7] << 8)
                if cadence != 0xFF:
                    self.data.cadence_rpm = cadence
                if power < 2000:
                    self.data.power_w = power
                self.data.last_pwr_ts = time.monotonic()
            elif page == 0x13:
                self.data.torque_eff_l   = _ant_pct(d[1])
                self.data.torque_eff_r   = _ant_pct(d[2])
                self.data.pedal_smooth_l = _ant_pct(d[3])
                self.data.pedal_smooth_r = _ant_pct(d[4])
        except Exception:
            pass

    def _on_rsc(self, data):
        """Broadcast from Running Speed & Cadence sensor (HRM-Pro+ ANT+ RSC).

        Page 0x01 (main page) layout:
          [0] page=0x01  [1-2] reserved  [3] stride count (cumulative)
          [4] cadence (strides/min, one foot)
          [5] speed fractional (1/256 m/s)  [6] speed integer (m/s)
          [7] flags (bit 0=running, bit 1=stride len valid)

        Stride length (if device supports it) may be in extended data pages
        or device-specific; validate from debug logs before parsing.
        """
        try:
            d = list(data)
            if len(d) < 8 or d[0] != 0x01:
                return
            cadence_raw = d[4]
            if cadence_raw != 0xFF:
                self.data.cadence_spm = cadence_raw * 2   # one foot → both feet
            speed_ms = d[6] + d[5] / 256.0
            log.debug("ANT+ RSC raw: %s  speed=%.2f m/s  cadence_spm=%d",
                      [hex(b) for b in d], speed_ms, cadence_raw * 2)
        except Exception:
            pass
