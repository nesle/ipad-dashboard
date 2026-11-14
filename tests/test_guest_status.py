"""Synthetic regression tests for guest status classification."""
import unittest
from unittest.mock import patch

from app import server


class GuestStatusTests(unittest.TestCase):
    def test_qemu_memory_does_not_trigger_alert(self):
        now = 2000000000
        rows = [
            {'object': 'qemu', 'vmid': '101', 'host': 'vm-example',
             'nodename': 'node-a', 'status': 'running', 'cpu': 0.1,
             'mem': 550, 'maxmem': 512, '_ts': now},
            {'object': 'lxc', 'vmid': '201', 'host': 'container-example',
             'nodename': 'node-a', 'status': 'running', 'cpu': 0.1,
             'mem': 95, 'maxmem': 100, '_ts': now},
        ]

        def query(measurement):
            return rows if measurement == 'system' else []

        with patch.object(server, 'query', side_effect=query), patch.object(server.time, 'time', return_value=now):
            result = server.collect()

        vm = result['guests']['qemu'][0]
        container = result['guests']['lxc'][0]
        self.assertEqual(vm['status'], 'healthy')
        self.assertGreater(vm['mem'], 100)
        self.assertEqual(container['status'], 'critical')

    def test_qemu_cpu_and_stopped_states_still_apply(self):
        self.assertEqual(server.status(85, None, None), 'critical')
        self.assertEqual(server.status(55, None, None), 'warning')
        self.assertEqual(server.status(5, None, None, running=False), 'stopped')


if __name__ == '__main__':
    unittest.main()
