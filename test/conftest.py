# SPDX-FileCopyrightText: 2022 Espressif Systems (Shanghai) CO LTD
# SPDX-License-Identifier: Apache-2.0

# pylint: disable=W0621  # redefined-outer-name

import logging
from enum import Enum
from typing import Any, Callable, Dict, Match, Optional, TextIO, Tuple

import pexpect
import pytest
# from _pytest.assertion import truncate
from _pytest.fixtures import FixtureRequest
from _pytest.monkeypatch import MonkeyPatch
from pytest_embedded_idf.app import IdfApp
from pytest_embedded_idf.dut import IdfDut
from pytest_embedded_idf.serial import IdfSerial
from pytest_embedded.plugin import multi_dut_argument


class Stages(Enum):
    STACK_DEFAULT = 1
    STACK_IPV4 = 2
    STACK_IPV6 = 3
    STACK_INIT = 4
    STACK_CONNECT = 5
    STACK_START = 6
    STACK_PAR_OK = 7
    STACK_PAR_FAIL = 8
    STACK_DESTROY = 9

DEFAULT_SDKCONFIG = 'default'


class ModbusTestDut(IdfDut):

    TEST_IP_PROMPT = r'Waiting IP([0-9]{1,2}) from stdin:\r\r\n'
    TEST_IP_SET_CONFIRM = r'.*IP\([0-9]+\) = \[([0-9a-zA-Z\.\:]+)\] set from stdin.*'
    TEST_IP_ADDRESS_REGEXP = r'.*example_connect: .* IPv4 [a-z]+:.* ([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}).*'
    TEST_APP_NAME = r'I \([0-9]+\) cpu_start: Project name:     ([_a-z]*)'

    TEST_EXPECT_STR_TIMEOUT = 40
    TEST_ACK_TIMEOUT = 1
    TEST_MAX_CIDS = 8

    app: IdfApp
    serial: IdfSerial

    def __init__(self, *args, **kwargs) -> None:  # type: ignore
        super().__init__(*args, **kwargs)
        self.logger = logging.getLogger()
        self.test_output: Optional[TextIO] = None
        self.ip_address: Optional[str] = None
        self.app_name: Optional[str] = None
        self.param_fail_count = 0
        self.param_ok_count = 0
        self.test_stage = Stages.STACK_DEFAULT
        self.dictionary = None
        self.test_finish = False
        self.test_status = False

    def close(self) -> None:
        super().close()

    def dut_get_ip(self) -> Optional[str]:
        if self.ip_address is None:
            expect_address = self.expect(self.TEST_IP_ADDRESS_REGEXP, timeout=self.TEST_EXPECT_STR_TIMEOUT)
            if isinstance(expect_address, Match):
                self.ip_address = expect_address.group(1).decode('ascii')
        return self.ip_address

    def dut_get_name(self) -> Optional[str]:
        if self.app_name is None:
            expect_name = self.expect(self.TEST_APP_NAME, timeout=self.TEST_EXPECT_STR_TIMEOUT)
            if isinstance(expect_name, Match):
                self.app_name = expect_name.group(1).decode('ascii')
        return self.app_name

    def dut_send_ip(self, slave_ip: Optional[str]) -> Optional[int]:
        ''' The function sends the slave IP address defined as a parameter to master
        '''
        addr_num = 0
        if isinstance(slave_ip, str):
            for addr_num in range(0, self.TEST_MAX_CIDS):
                try:
                    message = 'IP{}={}'.format(addr_num, slave_ip)
                    ack = self.expect(self.TEST_IP_PROMPT, timeout=self.TEST_ACK_TIMEOUT)
                    if isinstance(ack, Match) and (addr_num == int(ack.group(1))):
                        self.logger.info('{} sent to master'.format(message))
                        self.write(message)
                        message = r'IP({}) = [{}] set from stdin.'.format(addr_num, slave_ip)
                        self.expect_exact(message, timeout=self.TEST_ACK_TIMEOUT)
                        self.logger.info('IP{} was set correctly to {}'.format(addr_num, slave_ip))
                except pexpect.TIMEOUT:
                    self.logger.info('Send timeout: {}.'.format(message))
                    break
        return addr_num

    def expect_any(self, *expect_items: Tuple[str, Callable], timeout: Optional[int]) -> None:
        """
        expect_any(*expect_items, timeout=DEFAULT_TIMEOUT)
        expect any of the patterns.
        will call callback (if provided) if pattern match succeed and then return.
        will pass match result to the callback.

        :raise ExpectTimeout: failed to match any one of the expect items before timeout
        :raise UnsupportedExpectItem: pattern in expect_item is not string or compiled RegEx

        :arg expect_items: one or more expect items.
                           string, compiled RegEx pattern or (string or RegEx(string pattern), callback)
        :keyword timeout: timeout for expect
        :return: matched item
        """
        def process_expected_item(item_raw: Tuple[str, Callable[..., Any]]) -> Dict[str, Any]:
            # convert item raw data to standard dict
            item = {
                'pattern': item_raw[0] if isinstance(item_raw, tuple) else item_raw,
                'callback': item_raw[1] if isinstance(item_raw, tuple) else None,
                'index': -1,
                'ret': None,
            }
            return item

        expect_items_list = [process_expected_item(item) for item in expect_items]
        expect_patterns = [item['pattern'] for item in expect_items_list if item['pattern'] is not None]
        match_item = None

        match_index = self.pexpect_proc.expect(expect_patterns, timeout)
        if isinstance(match_index, int):
            match_item = expect_items_list[match_index]  # type: ignore
            match_item['index'] = match_index  # type: ignore
            if isinstance(self.pexpect_proc.match, Match) and len(self.pexpect_proc.match.groups()) > 0:
                match_item['ret'] = self.pexpect_proc.match.groups()
            if match_item['callback']:
                match_item['callback'](match_item['ret'])  # execution of callback function

    def dut_test_start(self, dictionary: Dict, timeout_value=TEST_EXPECT_STR_TIMEOUT) -> None:  # type: ignore
        """ The method to initialize and handle test stages
        """
        def handle_get_ip4(data: Optional[Any]) -> None:
            """ Handle get_ip v4
            """
            self.logger.info('%s[STACK_IPV4]: %s', self.dut_name, str(data))
            self.test_stage = Stages.STACK_IPV4

        def handle_get_ip6(data: Optional[Any]) -> None:
            """ Handle get_ip v6
            """
            self.logger.info('%s[STACK_IPV6]: %s', self.dut_name, str(data))
            self.test_stage = Stages.STACK_IPV6

        def handle_init(data: Optional[Any]) -> None:
            """ Handle init
            """
            self.logger.info('%s[STACK_INIT]: %s', self.dut_name, str(data))
            self.test_stage = Stages.STACK_INIT

        def handle_connect(data: Optional[Any]) -> None:
            """ Handle connect
            """
            self.logger.info('%s[STACK_CONNECT]: %s', self.dut_name, str(data))
            self.test_stage = Stages.STACK_CONNECT

        def handle_test_start(data: Optional[Any]) -> None:
            """ Handle connect
            """
            self.logger.info('%s[STACK_START]: %s', self.dut_name, str(data))
            self.test_stage = Stages.STACK_START

        def handle_par_ok(data: Optional[Any]) -> None:
            """ Handle parameter ok
            """
            self.logger.info('%s[READ_PAR_OK]: %s', self.dut_name, str(data))
            if self.test_stage.value >= Stages.STACK_START.value:
                self.param_ok_count += 1
            self.test_stage = Stages.STACK_PAR_OK

        def handle_par_fail(data: Optional[Any]) -> None:
            """ Handle parameter fail
            """
            self.logger.info('%s[READ_PAR_FAIL]: %s', self.dut_name, str(data))
            self.param_fail_count += 1
            self.test_stage = Stages.STACK_PAR_FAIL

        def handle_destroy(data: Optional[Any]) -> None:
            """ Handle destroy
            """
            self.logger.info('%s[%s]: %s', self.dut_name, Stages.STACK_DESTROY.name, str(data))
            self.test_stage = Stages.STACK_DESTROY

            self.test_finish = True

        while not self.test_finish:
            try:
                self.expect_any((dictionary[Stages.STACK_IPV4], handle_get_ip4),
                                (dictionary[Stages.STACK_IPV6], handle_get_ip6),
                                (dictionary[Stages.STACK_INIT], handle_init),
                                (dictionary[Stages.STACK_CONNECT], handle_connect),
                                (dictionary[Stages.STACK_START], handle_test_start),
                                (dictionary[Stages.STACK_PAR_OK], handle_par_ok),
                                (dictionary[Stages.STACK_PAR_FAIL], handle_par_fail),
                                (dictionary[Stages.STACK_DESTROY], handle_destroy),
                                timeout=timeout_value)
            except pexpect.TIMEOUT:
                self.logger.info('%s, expect timeout on stage %s (%s seconds)', self.dut_name, self.test_stage.name, timeout_value)
                self.test_finish = True


@pytest.fixture(scope='module')
def monkeypatch_module(request: FixtureRequest) -> MonkeyPatch:
    mp = MonkeyPatch()
    request.addfinalizer(mp.undo)
    return mp


@pytest.fixture(scope='module', autouse=True)
def replace_dut_class(monkeypatch_module: MonkeyPatch) -> None:
    monkeypatch_module.setattr('pytest_embedded_idf.dut.IdfDut', ModbusTestDut)


@pytest.fixture
@multi_dut_argument
def config(request: FixtureRequest) -> str:
    return getattr(request, 'param', None) or DEFAULT_SDKCONFIG
