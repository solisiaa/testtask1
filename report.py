#!/usr/bin/env python3
"""Расчёт условного распределения платежей; деньги хранятся в копейках."""
import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path


FIELDS = {
    'contracts': {'contract_id', 'contractor', 'amount', 'signed_at'},
    'works': {'work_id', 'contract_id', 'title', 'price', 'status', 'completed_at', 'cancelled_at'},
    'payments': {'payment_id', 'contract_id', 'work_id', 'purpose', 'amount', 'status', 'created_at', 'paid_at', 'refunded_at'},
}


def money(value):
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?', value):
        raise ValueError('Некорректная сумма: ' + repr(value))
    whole, _, fraction = value.partition('.')
    return int(whole) * 100 + int((fraction + '00')[:2])


def rub(value):
    sign = '-' if value < 0 else ''
    value = abs(value)
    return f'{sign}{value // 100}.{value % 100:02d}'


def read_data(directory):
    result = {}
    for name, required in FIELDS.items():
        with (Path(directory) / (name + '.csv')).open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if not required.issubset(reader.fieldnames or []):
                raise ValueError(f'{name}.csv: отсутствуют обязательные колонки')
            rows = list(reader)
        key = {'contracts': 'contract_id', 'works': 'work_id', 'payments': 'payment_id'}[name]
        seen = set()
        for row in rows:
            if any(row.get(field) is None for field in required) or None in row:
                raise ValueError(f'{name}.csv: неверное количество полей')
            if not row[key] or row[key] in seen:
                raise ValueError(f'{name}.csv: пустой или повторный идентификатор {row[key]!r}')
            seen.add(row[key])
        result[name] = rows
    return result


def validate(data):
    contracts = {row['contract_id']: row for row in data['contracts']}
    works = {row['work_id']: row for row in data['works']}
    for kind, rows in data.items():
        for row in rows:
            money(row['price'] if kind == 'works' else row['amount'])
            for key, value in row.items():
                if key.endswith('_at') and value:
                    date.fromisoformat(value)
            cid = row['contract_id']
            if cid not in contracts:
                raise ValueError('Неизвестный договор: ' + cid)
            signed = contracts[cid]['signed_at']
            if not signed:
                raise ValueError('Нет даты подписания: ' + cid)
            if kind == 'works':
                status = row['status']
                if status not in {'planned', 'done', 'cancelled'}:
                    raise ValueError('Неизвестный статус работы')
                if (status == 'done') != bool(row['completed_at']) or (status == 'cancelled') != bool(row['cancelled_at']):
                    raise ValueError('Статус работы противоречит датам: ' + row['work_id'])
                if any(row[k] and row[k] < signed for k in ('completed_at', 'cancelled_at')):
                    raise ValueError('Работа датирована раньше договора')
            if kind == 'payments':
                status = row['status']
                if status not in {'paid', 'queued', 'refunded'}:
                    raise ValueError('Неизвестный статус платежа')
                if not row['created_at'] or row['created_at'] < signed:
                    raise ValueError('Некорректная дата создания платежа')
                if (status != 'queued') != bool(row['paid_at']) or (status == 'refunded') != bool(row['refunded_at']):
                    raise ValueError('Статус платежа противоречит датам: ' + row['payment_id'])
                if row['paid_at'] and row['paid_at'] < row['created_at']:
                    raise ValueError('Оплата раньше создания платежа')
                if row['refunded_at'] and row['refunded_at'] < row['paid_at']:
                    raise ValueError('Возврат раньше оплаты')
                wid = row['work_id']
                if wid and (wid not in works or works[wid]['contract_id'] != cid):
                    raise ValueError('Платёж ссылается на чужую или неизвестную работу: ' + row['payment_id'])


def build_report(directory, as_of):
    cutoff = date.fromisoformat(as_of).isoformat()
    data = read_data(directory)
    validate(data)
    reports = []
    for contract in sorted(data['contracts'], key=lambda r: r['contract_id']):
        if contract['signed_at'] > cutoff:
            continue
        cid = contract['contract_id']
        works = []
        for row in data['works']:
            if row['contract_id'] != cid:
                continue
            status = 'planned'
            if row['completed_at'] and row['completed_at'] <= cutoff:
                status = 'done'
            if row['cancelled_at'] and row['cancelled_at'] <= cutoff:
                status = 'cancelled'
            works.append(dict(row, status=status, price=money(row['price']), direct=0, allocated=0))
        by_id = {row['work_id']: row for row in works}
        pool = paid = refunded = queued = 0
        for payment in data['payments']:
            if payment['contract_id'] != cid or payment['created_at'] > cutoff:
                continue
            amount = money(payment['amount'])
            if not payment['paid_at'] or payment['paid_at'] > cutoff:
                queued += amount
                continue
            paid += amount
            if payment['refunded_at'] and payment['refunded_at'] <= cutoff:
                refunded += amount
                continue
            if payment['work_id']:
                by_id[payment['work_id']]['direct'] += amount
            else:
                pool += amount
        # Сначала принятые работы по дате приёмки, затем плановые по ID.
        eligible = sorted((w for w in works if w['status'] != 'cancelled'),
                          key=lambda w: (w['status'] != 'done', w['completed_at'] if w['status'] == 'done' else '', w['work_id']))
        for work in eligible:
            allocation = min(pool, max(0, work['price'] - work['direct']))
            work['allocated'] = allocation
            pool -= allocation
        output = []
        for work in sorted(works, key=lambda w: w['work_id']):
            due = 0 if work['status'] == 'cancelled' else work['price']
            net_paid = work['direct'] + work['allocated']
            output.append({
                'work_id': work['work_id'], 'title': work['title'], 'status': work['status'],
                'price': rub(work['price']), 'payable': rub(due),
                'direct_paid': rub(work['direct']), 'allocated_paid': rub(work['allocated']),
                'paid': rub(net_paid), 'remaining': rub(max(0, due - net_paid)),
                'overpaid': rub(max(0, net_paid - due)),
            })
        active = sum(w['price'] for w in eligible)
        reports.append({
            'contract_id': cid, 'contractor': contract['contractor'],
            'contract_amount': rub(money(contract['amount'])),
            'works_price': rub(sum(w['price'] for w in works)), 'active_works_amount': rub(active),
            'contract_minus_active': rub(money(contract['amount']) - active),
            'gross_paid': rub(paid), 'refunded': rub(refunded), 'paid': rub(paid - refunded),
            'queued': rub(queued), 'unallocated': rub(pool),
            'remaining': rub(sum(money(w['remaining']) for w in output)),
            'overpaid': rub(sum(money(w['overpaid']) for w in output)),
            'net_balance': rub(active - (paid - refunded)), 'works': output,
        })
    return {'as_of': cutoff, 'currency': 'RUB', 'allocation_is_assumed': True, 'contracts': reports}


def render_text(report):
    lines = [f"Отчёт на {report['as_of']} (RUB)",
             'Распределение общих платежей условное; «осталось» включает плановые работы.']
    for c in report['contracts']:
        lines += ['', f"{c['contract_id']} — {c['contractor']}",
                  'Работа | Статус | Цена | К оплате | Выплачено | Осталось | Переплата']
        for w in c['works']:
            lines.append(' | '.join(w[k] for k in ('work_id', 'status', 'price', 'payable', 'paid', 'remaining', 'overpaid')) + ' — ' + w['title'])
        labels = [('contract_amount', 'Сумма договора'), ('works_price', 'Цены всех работ'),
                  ('active_works_amount', 'Действующие работы'), ('contract_minus_active', 'Договор минус действующие работы'),
                  ('gross_paid', 'Ушло денег'), ('refunded', 'Возвращено'), ('paid', 'Выплачено за вычетом возвратов'),
                  ('queued', 'В очереди на дату'), ('remaining', 'Осталось по работам'),
                  ('overpaid', 'Переплата по работам'), ('unallocated', 'Нераспределённый аванс'), ('net_balance', 'Чистый остаток по действующим работам')]
        lines.extend(f'{label}: {c[key]}' for key, label in labels)
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', required=True, help='Дата отчёта включительно, YYYY-MM-DD')
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parent / 'data')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    args = parser.parse_args()
    try:
        report = build_report(args.data_dir, args.date)
    except (ValueError, OSError, csv.Error) as exc:
        print(f'Ошибка: {exc}', file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.format == 'json' else render_text(report), end='\n' if args.format == 'json' else '')
    return 0


if __name__ == '__main__':
    sys.exit(main())
