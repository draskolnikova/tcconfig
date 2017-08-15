# encoding: utf-8

"""
.. codeauthor:: Tsuyoshi Hombashi <tsuyoshi.hombashi@gmail.com>
"""

from __future__ import absolute_import
from __future__ import unicode_literals

import copy

import simplesqlite
import subprocrunner
import typepy
from typepy.type import Integer

from .._common import (
    is_anywhere_network,
    run_tc_show,
)
from .._const import (
    Tc,
    TrafficDirection,
)
from .._iptables import IptablesMangleController
from ._class import TcClassParser
from ._filter import TcFilterParser
from ._qdisc import TcQdiscParser


class TcShapingRuleParser(object):
    @property
    def device(self):
        return self.__device

    def __init__(self, device, ip_version, logger):
        self.__con = simplesqlite.connect_sqlite_memdb()
        self.__device = device
        self.__ip_version = ip_version
        self.__logger = logger

        self.__filter_parser = TcFilterParser(self.__con, self.__ip_version)
        self.__ifb_device = self.__get_ifb_from_device()

        self.__iptables_ctrl = IptablesMangleController(True, ip_version)

    def get_tc_parameter(self):
        return {
            self.device: {
                TrafficDirection.OUTGOING: self.__get_shaping_rule(
                    self.device),
                TrafficDirection.INCOMING: self.__get_shaping_rule(
                    self.__ifb_device),
            },
        }

    def get_outgoing_tc_filter(self):
        return self.__parse_tc_filter(self.device)

    def get_incoming_tc_filter(self):
        if not self.__ifb_device:
            return []

        return self.__parse_tc_filter(self.__ifb_device)

    def __get_ifb_from_device(self):
        filter_runner = subprocrunner.SubprocessRunner(
            "tc filter show dev {:s} root".format(self.device), dry_run=False)
        filter_runner.run()

        return self.__filter_parser.parse_incoming_device(filter_runner.stdout)

    def __get_filter_key(self, filter_param):
        src_network_format = Tc.Param.SRC_NETWORK + "={:s}"
        dst_network_format = Tc.Param.DST_NETWORK + "={:s}"
        protocol_format = Tc.Param.PROTOCOL + "={:s}"
        key_item_list = []

        if Tc.Param.HANDLE in filter_param:
            handle = filter_param.get(Tc.Param.HANDLE)
            Integer(handle).validate()
            handle = int(handle)

            for mangle in self.__iptables_ctrl.parse():
                if mangle.mark_id != handle:
                    continue

                key_item_list.append(
                    dst_network_format.format(mangle.destination))
                if typepy.is_not_null_string(mangle.source):
                    key_item_list.append("{:s}={:s}".format(
                        Tc.Param.SRC_NETWORK, mangle.source))
                key_item_list.append(protocol_format.format(mangle.protocol))

                break
            else:
                raise ValueError("mangle mark not found: {}".format(mangle))
        else:
            src_network = filter_param.get(Tc.Param.SRC_NETWORK)
            if (typepy.is_not_null_string(src_network) and
                    not is_anywhere_network(src_network, self.__ip_version)):
                key_item_list.append(src_network_format.format(src_network))

            dst_network = filter_param.get(Tc.Param.DST_NETWORK)
            if typepy.is_not_null_string(dst_network):
                key_item_list.append(dst_network_format.format(dst_network))

            src_port = filter_param.get(Tc.Param.SRC_PORT)
            if Integer(src_port).is_type():
                port_format = Tc.Param.SRC_PORT + "={:d}"
                key_item_list.append(port_format.format(src_port))

            dst_port = filter_param.get(Tc.Param.DST_PORT)
            if Integer(dst_port).is_type():
                port_format = Tc.Param.DST_PORT + "={:d}"
                key_item_list.append(port_format.format(dst_port))

            protocol = filter_param.get(Tc.Param.PROTOCOL)
            if typepy.is_not_null_string(protocol):
                key_item_list.append(protocol_format.format(protocol))

        return ", ".join(key_item_list)

    def __get_shaping_rule(self, device):
        if typepy.is_null_string(device):
            return {}

        class_param_list = self.__parse_tc_class(device)
        filter_param_list = self.__parse_tc_filter(device)
        qdisc_param_list = self.__parse_tc_qdisc(device)

        shaping_rule_mapping = {}

        for filter_param in filter_param_list:
            self.__logger.debug(
                "{:s} param: {}".format(Tc.Subcommand.FILTER, filter_param))
            shaping_rule = {}

            filter_key = self.__get_filter_key(filter_param)
            if typepy.is_null_string(filter_key):
                self.__logger.debug(
                    "empty filter key: {}".format(filter_param))
                continue

            for qdisc_param in qdisc_param_list:
                self.__logger.debug(
                    "{:s} param: {}".format(Tc.Subcommand.QDISC, qdisc_param))

                if qdisc_param.get(Tc.Param.PARENT) not in (
                        filter_param.get(Tc.Param.FLOW_ID),
                        filter_param.get(Tc.Param.CLASS_ID)):
                    continue

                shaping_rule.update(self.__strip_qdisc_param(qdisc_param))

            for class_param in class_param_list:
                self.__logger.debug(
                    "{:s} param: {}".format(Tc.Subcommand.CLASS, class_param))

                if class_param.get(Tc.Param.CLASS_ID) not in (
                        filter_param.get(Tc.Param.FLOW_ID),
                        filter_param.get(Tc.Param.CLASS_ID)):
                    continue

                work_class_param = copy.deepcopy(class_param)
                del work_class_param[Tc.Param.CLASS_ID]
                shaping_rule.update(work_class_param)

            if not shaping_rule:
                self.__logger.debug(
                    "shaping rule not found for '{}'".format(filter_param))
                continue

            self.__logger.debug(
                "shaping rule found: {} {}".format(filter_key, shaping_rule))

            shaping_rule_mapping[filter_key] = shaping_rule

        return shaping_rule_mapping

    def __parse_tc_qdisc(self, device):
        return TcQdiscParser(self.__con).parse(
            device, run_tc_show(Tc.Subcommand.QDISC, device))

    def __parse_tc_filter(self, device):
        return self.__filter_parser.parse(
            device, run_tc_show(Tc.Subcommand.FILTER, device))

    def __parse_tc_class(self, device):
        return TcClassParser(self.__con).parse(
            device, run_tc_show(Tc.Subcommand.CLASS, device))

    @staticmethod
    def __strip_qdisc_param(qdisc_param):
        work_qdisc_param = copy.deepcopy(qdisc_param)

        try:
            del work_qdisc_param[Tc.Param.PARENT]
        except KeyError:
            pass

        try:
            del work_qdisc_param[Tc.Param.HANDLE]
        except KeyError:
            pass

        return work_qdisc_param
