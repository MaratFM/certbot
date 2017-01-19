"""Tests for certbot.plugins.standalone."""
import argparse
import socket
import unittest

import mock
import six

from acme import challenges
from acme import jose

from certbot import achallenges
from certbot import errors

from certbot.tests import acme_util
from certbot.tests import util as test_util


class ServerManagerTest(unittest.TestCase):
    """Tests for certbot.plugins.standalone.ServerManager."""

    def setUp(self):
        from certbot.plugins.standalone import ServerManager
        self.certs = {}
        self.http_01_resources = {}
        self.mgr = ServerManager(self.certs, self.http_01_resources)

    def test_init(self):
        self.assertTrue(self.mgr.certs is self.certs)
        self.assertTrue(
            self.mgr.http_01_resources is self.http_01_resources)

    def _test_run_stop(self, challenge_type):
        server = self.mgr.run(port=0, challenge_type=challenge_type)
        port = server.socket.getsockname()[1]  # pylint: disable=no-member
        self.assertEqual(self.mgr.running(), {port: server})
        self.mgr.stop(port=port)
        self.assertEqual(self.mgr.running(), {})

    def test_run_stop_tls_sni_01(self):
        self._test_run_stop(challenges.TLSSNI01)

    def test_run_stop_http_01(self):
        self._test_run_stop(challenges.HTTP01)

    def test_run_idempotent(self):
        server = self.mgr.run(port=0, challenge_type=challenges.HTTP01)
        port = server.socket.getsockname()[1]  # pylint: disable=no-member
        server2 = self.mgr.run(port=port, challenge_type=challenges.HTTP01)
        self.assertEqual(self.mgr.running(), {port: server})
        self.assertTrue(server is server2)
        self.mgr.stop(port)
        self.assertEqual(self.mgr.running(), {})

    def test_run_bind_error(self):
        some_server = socket.socket()
        some_server.bind(("", 0))
        port = some_server.getsockname()[1]
        self.assertRaises(
            errors.StandaloneBindError, self.mgr.run, port,
            challenge_type=challenges.HTTP01)
        self.assertEqual(self.mgr.running(), {})


class SupportedChallengesValidatorTest(unittest.TestCase):
    """Tests for plugins.standalone.supported_challenges_validator."""

    def setUp(self):
        self.set_by_cli_patch = mock.patch(
            "certbot.plugins.standalone.cli.set_by_cli")
        self.stderr_patch = mock.patch("certbot.plugins.standalone.sys.stderr")

        self.set_by_cli_patch.start().return_value = True
        self.stderr = self.stderr_patch.start()

    def tearDown(self):
        self.set_by_cli_patch.stop()
        self.stderr_patch.stop()

    def _call(self, data):
        from certbot.plugins.standalone import (
            supported_challenges_validator)
        return_value = supported_challenges_validator(data)
        self.assertTrue(self.stderr.write.called)  # pylint: disable=no-member
        self.stderr.write.reset_mock()  # pylint: disable=no-member
        return return_value

    def test_correct(self):
        self.assertEqual("tls-sni-01", self._call("tls-sni-01"))
        self.assertEqual("http-01", self._call("http-01"))
        self.assertEqual("tls-sni-01,http-01", self._call("tls-sni-01,http-01"))
        self.assertEqual("http-01,tls-sni-01", self._call("http-01,tls-sni-01"))

    def test_unrecognized(self):
        assert "foo" not in challenges.Challenge.TYPES
        self.assertRaises(argparse.ArgumentTypeError, self._call, "foo")

    def test_not_subset(self):
        self.assertRaises(argparse.ArgumentTypeError, self._call, "dns")

    def test_dvsni(self):
        self.assertEqual("tls-sni-01", self._call("dvsni"))
        self.assertEqual("http-01,tls-sni-01", self._call("http-01,dvsni"))
        self.assertEqual("tls-sni-01,http-01", self._call("dvsni,http-01"))


def get_open_port():
    """Gets an open port number from the OS."""
    open_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
    open_socket.bind(("", 0))
    port = open_socket.getsockname()[1]
    open_socket.close()
    return port


class AuthenticatorTest(unittest.TestCase):
    """Tests for certbot.plugins.standalone.Authenticator."""

    def setUp(self):
        from certbot.plugins.standalone import Authenticator

        self.config = mock.MagicMock(
            tls_sni_01_port=get_open_port(), http01_port=get_open_port(),
            standalone_supported_challenges="tls-sni-01,http-01")
        self.auth = Authenticator(self.config, name="standalone")

    def test_supported_challenges(self):
        self.assertEqual(self.auth.supported_challenges,
                         [challenges.TLSSNI01, challenges.HTTP01])

    def test_supported_challenges_configured(self):
        self.config.standalone_supported_challenges = "tls-sni-01"
        self.assertEqual(self.auth.supported_challenges,
                         [challenges.TLSSNI01])

    def test_more_info(self):
        self.assertTrue(isinstance(self.auth.more_info(), six.string_types))

    def test_get_chall_pref(self):
        self.assertEqual(self.auth.get_chall_pref(domain=None),
                         [challenges.TLSSNI01, challenges.HTTP01])

    def test_get_chall_pref_configured(self):
        self.config.standalone_supported_challenges = "tls-sni-01"
        self.assertEqual(self.auth.get_chall_pref(domain=None),
                         [challenges.TLSSNI01])

    def test_perform(self):
        achalls = self._get_achalls()
        self.auth.servers = mock.MagicMock()

        response = self.auth.perform(achalls)

        expected = [achall.response(achall.account_key) for achall in achalls]
        self.assertEqual(response, expected)

    def test_perform_eacces(self):
        achalls = self._setup_perform_error(socket.errno.EACCES)
        self.assertRaises(errors.PluginError, self.auth.perform, achalls)

    @test_util.patch_get_utility()
    def test_perform_eaddrinuse_no_retry(self, mock_get_utility):
        achalls = self._setup_perform_error(socket.errno.EADDRINUSE)
        mock_yesno = mock_get_utility.return_value.yesno
        mock_yesno.return_value = False

        self.assertRaises(errors.PluginError, self.auth.perform, achalls)
        yesno_args, yesno_kwargs = mock_yesno.call_args
        self.assertTrue("in use" in yesno_args[0])
        self.assertFalse(yesno_kwargs.get("default", True))

    def test_perform_unexpected_socket_error(self):
        achalls = self._setup_perform_error(socket.errno.ENOTCONN)
        self.assertRaises(
            errors.StandaloneBindError, self.auth.perform, achalls)

    def _setup_perform_error(self, errno):
        self.auth.servers = mock.MagicMock()
        socket_error = mock.MagicMock(errno=errno)
        error = errors.StandaloneBindError(socket_error, -1)
        self.auth.servers.run.side_effect = error
        return self._get_achalls()

    @classmethod
    def _get_achalls(cls):
        domain = b'localhost'
        key = jose.JWK.load(test_util.load_vector('rsa512_key.pem'))
        http_01 = achallenges.KeyAuthorizationAnnotatedChallenge(
            challb=acme_util.HTTP01_P, domain=domain, account_key=key)
        tls_sni_01 = achallenges.KeyAuthorizationAnnotatedChallenge(
            challb=acme_util.TLSSNI01_P, domain=domain, account_key=key)

        return [http_01, tls_sni_01]

    def test_cleanup(self):
        self.auth.servers = mock.Mock()
        self.auth.servers.running.return_value = {
            1: "server1",
            2: "server2",
        }
        self.auth.served["server1"].add("chall1")
        self.auth.served["server2"].update(["chall2", "chall3"])

        self.auth.cleanup(["chall1"])
        self.assertEqual(self.auth.served, {
            "server1": set(), "server2": set(["chall2", "chall3"])})
        self.auth.servers.stop.assert_called_once_with(1)

        self.auth.servers.running.return_value = {
            2: "server2",
        }
        self.auth.cleanup(["chall2"])
        self.assertEqual(self.auth.served, {
            "server1": set(), "server2": set(["chall3"])})
        self.assertEqual(1, self.auth.servers.stop.call_count)

        self.auth.cleanup(["chall3"])
        self.assertEqual(self.auth.served, {
            "server1": set(), "server2": set([])})
        self.auth.servers.stop.assert_called_with(2)

if __name__ == "__main__":
    unittest.main()  # pragma: no cover
