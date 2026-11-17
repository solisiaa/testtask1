import csv
import tempfile
import unittest
from pathlib import Path

from report import build_report, money, rub


DATA = Path(__file__).resolve().parents[1] / 'data'


class ReportTests(unittest.TestCase):
    def report(self, day='2026-11-10', directory=DATA):
        return {c['contract_id']: c for c in build_report(directory, day)['contracts']}

    def test_control_date(self):
        contracts = self.report()
        c1, c2 = contracts['D-1'], contracts['D-2']
        self.assertEqual((c1['paid'], c1['remaining'], c1['queued']), ('315000.00', '185000.00', '150000.00'))
        self.assertEqual(c1['contract_minus_active'], '100000.00')
        works = {w['work_id']: w for w in c1['works']}
        self.assertEqual([works[k]['paid'] for k in ('W-11', 'W-12', 'W-13', 'W-14', 'W-15', 'W-16')],
                         ['100000.00', '100000.00', '0.00', '100000.00', '15000.00', '0.00'])
        self.assertEqual(works['W-13']['payable'], '0.00')
        self.assertEqual((c2['paid'], c2['remaining']), ('75000.00', '15000.00'))
        self.assertEqual(c2['works'][2]['status'], 'planned')
        self.assertEqual(c2['works'][2]['paid'], '25000.00')

    def test_refund_and_payment_boundaries(self):
        self.assertEqual(self.report('2026-09-01')['D-1']['paid'], '300000.00')
        self.assertEqual(self.report('2026-09-02')['D-1']['paid'], '350000.00')
        self.assertEqual(self.report('2026-09-09')['D-1']['paid'], '350000.00')
        self.assertEqual(self.report('2026-09-10')['D-1']['paid'], '300000.00')
        self.assertEqual(self.report('2026-11-11')['D-2']['works'][2]['status'], 'done')
        c2 = self.report('2026-11-12')['D-2']
        self.assertEqual((c2['paid'], c2['remaining'], c2['unallocated'], c2['net_balance']),
                         ('95000.00', '0.00', '5000.00', '-5000.00'))

    def test_cancellation_and_signed_boundaries(self):
        self.assertEqual(self.report('2026-10-11')['D-1']['active_works_amount'], '600000.00')
        self.assertEqual(self.report('2026-10-12')['D-1']['active_works_amount'], '500000.00')
        self.assertEqual(self.report('2026-08-24'), {})
        self.assertIn('D-1', self.report('2026-08-25'))

    def test_reconciliation_on_all_event_dates(self):
        days = {'2026-11-10'}
        for path in DATA.glob('*.csv'):
            with path.open(newline='', encoding='utf-8') as stream:
                for row in csv.DictReader(stream):
                    days.update(v for k, v in row.items() if k.endswith('_at') and v)
        for day in days:
            for c in self.report(day).values():
                with self.subTest(day=day, contract=c['contract_id']):
                    total = sum(money(w['paid']) for w in c['works']) + money(c['unallocated'])
                    self.assertEqual(total, money(c['paid']))
                    self.assertEqual(money(c['gross_paid']) - money(c['refunded']), total)
                    balance = money(c['remaining']) - money(c['overpaid']) - money(c['unallocated'])
                    self.assertEqual(rub(balance), c['net_balance'])

    def altered(self, table, mutate):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        for source in DATA.glob('*.csv'):
            with source.open(encoding='utf-8', newline='') as stream:
                reader = csv.DictReader(stream)
                fields, rows = reader.fieldnames, list(reader)
            if source.stem == table:
                mutate(rows)
            with (directory / source.name).open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
        return directory

    def test_direct_overpayment_is_not_moved_to_other_works(self):
        directory = self.altered('payments', lambda rows: rows[2].update(amount='120000.01'))
        c1 = self.report(directory=directory)['D-1']
        self.assertEqual(c1['works'][3]['overpaid'], '20000.01')
        self.assertEqual(c1['works'][3]['allocated_paid'], '0.00')
        self.assertEqual(c1['remaining'], '100000.00')

    def test_payment_to_cancelled_work_stays_visible(self):
        directory = self.altered('payments', lambda rows: rows[2].update(work_id='W-13'))
        work = self.report(directory=directory)['D-1']['works'][2]
        self.assertEqual((work['paid'], work['overpaid'], work['remaining']), ('15000.00', '15000.00', '0.00'))

    def test_cent_and_stable_order(self):
        directory = self.altered('payments', lambda rows: rows[0].update(amount='300000.01'))
        self.assertEqual(self.report(directory=directory)['D-1']['works'][4]['paid'], '15000.01')
        directory = self.altered('works', lambda rows: rows.reverse())
        self.assertEqual(self.report(directory=directory), self.report())

    def test_invalid_records_rejected(self):
        cases = [
            ('payments', lambda rows: rows[2].update(work_id='W-21')),
            ('payments', lambda rows: rows[0].update(paid_at='')),
            ('payments', lambda rows: rows[1].update(refunded_at='2026-09-01')),
            ('payments', lambda rows: rows.append(dict(rows[0]))),
            ('works', lambda rows: rows[0].update(price='1.001')),
            ('works', lambda rows: rows[0].update(status='unknown')),
        ]
        for table, mutate in cases:
            with self.subTest(table=table, mutate=mutate):
                with self.assertRaises(ValueError):
                    self.report(directory=self.altered(table, mutate))

    def test_money(self):
        self.assertEqual(money('0.01'), 1)
        self.assertEqual(rub(-1), '-0.01')
        for value in ('nan', '1.001', '-1', '1e3'):
            with self.assertRaises(ValueError):
                money(value)


if __name__ == '__main__':
    unittest.main()
