"""Explainable stock and replenishment calculations in existing inventory units."""
from __future__ import annotations
import calendar
import math
import unicodedata
from datetime import date, datetime, timedelta

ITEMS = {
    'Кофе': 'кг', 'Молоко': 'кг', 'Мока': 'кг', 'Раф банан': 'кг', 'Шоколад': 'кг',
    'Стаканы': 'шт', 'Сахар': 'стиков', 'Сиропы': 'бут',
    'Крышки бел': 'шт', 'Крышки чёрн': 'шт', 'Трубочки': 'шт',
    'Палочки': 'шт', 'Манжеты': 'шт', 'Вода': 'бут',
    'Влажные салф': 'уп', 'Мус.пакеты': 'рулонов', 'Салфетки сухие': 'шт',
}
POINTS = {'Беломорский': 52309, 'Гагарина': 52229, 'Гиппо': 52307,
          'Макси': 52310, 'Сити': 52308, 'Южный': 52311}
RECIPE_ITEMS = ('Кофе', 'Молоко', 'Шоколад', 'Раф банан')


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(str(value).replace('\xa0', '').replace(' ', '').replace(',', '.'))
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError):
        return None


def day(value):
    for fmt in ('%Y-%m-%d', '%d.%m.%Y'):
        try:
            return datetime.strptime(str(value), fmt).date()
        except ValueError:
            pass
    return None


def norm(value):
    # Only explicit spelling variants, never fuzzy product matching.
    value = unicodedata.normalize('NFKC', str(value)).casefold().replace('ё', 'е')
    return ' '.join(value.split()).replace('моккачино', 'мокачино').replace('раф с бананом', 'банановый раф')


def recipe_index(rows, costing_rows=None):
    result = {}
    for row in rows:
        if len(row) < 13 or str(row[0]) not in POINTS:
            continue
        amounts = [number(row[i]) for i in (9, 10, 11, 12)]
        if any(x is None for x in amounts):
            continue
        key = (str(row[0]), norm(row[1]))
        if key in result:
            raise ValueError('Повторная рецептура для точки и напитка')
        result[key] = dict(zip(RECIPE_ITEMS, (x / 1000 for x in amounts)))
    if costing_rows:
        final = {}
        for row in costing_rows:
            if len(row) < 6 or str(row[0]) not in POINTS:
                continue
            amounts = [number(row[i]) for i in (2, 3, 4, 5)]
            if any(x is None for x in amounts):
                continue
            key = (str(row[0]), norm(row[1]))
            if key in final:
                raise ValueError('Повторный напиток в итоговой себестоимости')
            final[key] = dict(zip(RECIPE_ITEMS, (x / 1000 for x in amounts)))
        if final:
            for key, values in final.items():
                if key in result and any(abs(values[item] - result[key][item]) > 0.000011 for item in RECIPE_ITEMS):
                    raise ValueError('Рецептура расходится с итоговой себестоимостью: ' + str(key))
            result = final
    # Подтверждение владельца 02.11.2026: эспрессо — одна таблетка 10 г.
    for point in ('Южный', 'Макси'):
        result[(point, norm('Эспрессо'))] = {'Кофе': 0.01, 'Молоко': 0.0,
                                            'Шоколад': 0.0, 'Раф банан': 0.0}
    return result


def consumption(sales, recipes, *, strict=True):
    """Unknown drinks invalidate ingredient consumption, but not counted cups."""
    totals = {item: 0.0 for item in (*RECIPE_ITEMS, 'Стаканы')}
    unmatched = []
    for row in sales.get('products', []):
        count = number(row.get('sales_count'))
        if count is None:
            raise ValueError('Некорректное количество напитков')
        totals['Стаканы'] += count
        recipe = recipes.get((sales['point'], norm(row.get('name', ''))))
        if recipe is None:
            unmatched.append(str(row.get('name', '')))
        else:
            for item in RECIPE_ITEMS:
                totals[item] += count * recipe[item]
    if unmatched and strict:
        for item in RECIPE_ITEMS:
            totals[item] = None
    return totals, unmatched


def latest_service_reports(rows, active_points):
    """Operational visit signals; a blank stock field is not a revision count."""
    latest = {}
    for row in rows:
        point = row.get('Точка')
        observed = day(row.get('Дата'))
        if point not in active_points or observed is None:
            continue
        key = (observed, int(row.get('_row') or 0))
        if point not in latest or key > latest[point][0]:
            latest[point] = (key, {'point': point, 'date': observed.isoformat(),
                                   'water': number(row.get('Вода(бут)')),
                                   'shortage': str(row.get('Нехватка') or '').strip(),
                                   'stock_note': str(row.get('Остатки') or '').strip(),
                                   'source_row': row.get('_row')})
    return [latest[p][1] for p in active_points if p in latest]


def build_dashboard(raw, policies, orders, *, today=None, month=None):
    today = today or date.today()
    month = month or today.strftime('%Y-%m')
    target = date.fromisoformat(month + '-01')
    month_days = calendar.monthrange(target.year, target.month)[1]
    recipes = recipe_index(raw.get('recipes', []), raw.get('costing', []))
    service_reports = latest_service_reports(raw.get('service_reports', []), raw.get('active_points', list(POINTS)))
    for report in service_reports:
        report['photo_rows'] = [p['row'] for p in raw.get('photos', [])
                                if p['point'] == report['point'] and p['date'] == report['date']]
    revisions = raw.get('revisions', [])
    latest = {}
    periods = sorted({r.get('Период', '') for r in revisions},
                     key=lambda p: datetime.strptime(p, '%m.%Y') if len(p) == 7 and p[2] == '.' else datetime.min)
    selected_period = periods[-1] if periods else None
    for row in revisions:
        if row.get('Период') != selected_period:
            continue
        location = row.get('Локация', '')
        if location in latest:
            # Never silently choose one of duplicate point/month snapshots.
            latest[location] = {'Локация': location, '_duplicate': True}
        else:
            latest[location] = row
    manual_counts = {}
    for count in raw.get('manual_counts', []):
        counted_on = day(count.get('counted_on'))
        key = (count.get('item'), count.get('location'))
        if key[0] in ITEMS and key[1] in (*POINTS, 'Дома', 'Гараж') and counted_on and counted_on <= today and number(count.get('quantity')) is not None:
            if key not in manual_counts or counted_on >= day(manual_counts[key]['counted_on']):
                manual_counts[key] = count
            latest.setdefault(key[1], {'Локация': key[1], '_item_dates': {}, '_item_sources': {}})
    sales_by_point = {}
    history_by_point = {}
    for sale in raw.get('sales', []):
        totals, missing = consumption(sale, recipes)
        days = (day(sale['until']) - day(sale['since'])).days + 1
        if days < 1:
            continue
        history_by_point[sale['point']] = (sale, totals, missing, days)
    for sale in raw.get('since_revision_sales', []):
        totals, missing = consumption(sale, recipes)
        sales_by_point[sale['point']] = (sale, totals, missing)
    warnings = list(raw.get('warnings', []))
    rows = []
    movement_rows = list(raw.get('movements', []))
    for order in orders:
        if order['status'] == 'received' and order.get('received_on') and order.get('location'):
            duplicate = any(m.get('item') == order['item'] and m.get('point') == order['location']
                            and day(m.get('date')) == day(order['received_on'])
                            and number(m.get('quantity')) == order['quantity'] for m in movement_rows)
            movement_rows.append({'item': order['item'], 'point': order['location'],
                                  'date': order['received_on'], 'source': 'Поставщик',
                                  'quantity': None if duplicate else order['quantity']})
    for item, unit in ITEMS.items():
        policy = policies.get(item, {})
        issues = []
        locations = []
        forecast_parts, actual_parts, baseline_parts, known_parts = [], [], [], []
        for location, snapshot in latest.items():
            revision_q = number(snapshot.get(item))
            revision_date = day(snapshot.get('_item_dates', {}).get(item) if snapshot.get('_from_bdr') else snapshot.get('Дата заполнения'))
            manual = manual_counts.get((item, location))
            if manual and revision_date and day(manual['counted_on']) < revision_date:
                manual = None
            q = number(manual['quantity']) if manual else revision_q
            counted = day(manual['counted_on']) if manual else revision_date
            current, used = None, None
            notes = []
            if item in snapshot.get('_conflicts', {}):
                conflict = snapshot['_conflicts'][item]
                notes.append(f'Расхождение источников: БДР {conflict["bdr"]}, журнал бота {conflict["primary"]}')
            if snapshot.get('_duplicate'):
                notes.append('Несколько ревизий места за месяц')
            if revision_q is not None:
                baseline_parts.append(revision_q)
            if q is not None and counted and counted <= today:
                incoming, outgoing = 0.0, 0.0
                ambiguous = False
                for movement in movement_rows:
                    event_day = day(movement.get('date'))
                    if movement.get('item') != item or not event_day or event_day < counted or event_day > today:
                        continue
                    involved = movement.get('point') == location or movement.get('source') == location
                    if involved and event_day == counted:
                        ambiguous = True
                        continue
                    amount = number(movement.get('quantity'))
                    if amount is None:
                        if involved:
                            ambiguous = True
                        continue
                    if movement.get('point') == location:
                        incoming += amount
                    if movement.get('source') == location:
                        outgoing += amount
                if ambiguous:
                    notes.append('Неясные единицы или порядок движений в день ревизии')
                if location in ('Дома', 'Гараж'):
                    used = 0.0
                elif manual and counted >= today - timedelta(days=1):
                    used = 0.0
                elif manual and location in history_by_point:
                    history_sale = history_by_point[location][0]
                    if (history_sale.get('daily_complete') and day(history_sale.get('since')) <= counted + timedelta(days=1)
                            and day(history_sale.get('until')) == today - timedelta(days=1)):
                        product_counts = {}
                        for sold_date, products in history_sale.get('daily_products', {}).items():
                            sold = day(sold_date)
                            if sold and counted < sold < today:
                                for name, amount in products.items():
                                    product_counts[name] = product_counts.get(name, 0) + amount
                        values, missing = consumption({'point': location, 'products': [
                            {'name': name, 'sales_count': amount} for name, amount in product_counts.items()]}, recipes)
                        used = values.get(item)
                        if missing and item in RECIPE_ITEMS:
                            notes.append('Не сопоставлены напитки после подсчёта: ' + ', '.join(missing))
                elif location in sales_by_point:
                    sale, values, missing = sales_by_point[location]
                    expected_since = counted + timedelta(days=1)
                    if day(sale['since']) == expected_since and day(sale['until']) == today - timedelta(days=1):
                        used = values.get(item)
                        if missing and item in RECIPE_ITEMS:
                            notes.append('Не сопоставлены напитки: ' + ', '.join(missing))
                elif counted >= today - timedelta(days=1):
                    used = 0.0
                if used is not None and not ambiguous:
                    current = q + incoming - outgoing - used
                    actual_parts.append(current)
                elif location not in ('Дома', 'Гараж'):
                    notes.append('Нет полного расхода после ревизии')
            elif q is not None:
                notes.append('Неизвестна или некорректна дата подсчёта')
            if q is None:
                notes.append('Остаток не указан')
            history = history_by_point.get(location)
            daily = None
            if history:
                daily = history[1].get(item) if location in raw.get('active_points', POINTS) else None
                daily = daily / history[3] if daily is not None else None
                if daily is not None:
                    forecast_parts.append(daily)
                if location in raw.get('active_points', POINTS):
                    known_values, _ = consumption(history[0], recipes, strict=False)
                    if item in known_values:
                        known_parts.append(known_values[item] / history[3])
            locations.append({'name': location, 'counted_at': revision_date.isoformat() if revision_date else None,
                              'source': snapshot.get('_item_sources', {}).get(item, 'Первичная ревизия бота'),
                              'revision': revision_q, 'manual_count': q if manual else None,
                              'manual_counted_at': counted.isoformat() if manual else None,
                              'manual_note': manual.get('note', '') if manual else None,
                              'estimated': round(current, 3) if current is not None else None,
                              'consumed': round(used, 3) if used is not None else None,
                              'daily': round(daily, 4) if daily is not None else None, 'issues': notes})
        missing_places = [p for p in [*raw.get('active_points', POINTS), 'Дома'] if p not in latest]
        if missing_places:
            issues.append('Нет ревизии: ' + ', '.join(missing_places))
        if any(p not in history_by_point for p in raw.get('active_points', POINTS)):
            issues.append('Продажи за период прогноза получены не по всем точкам')
        active_count = len(raw.get('active_points', POINTS))
        daily = sum(forecast_parts) if len(forecast_parts) == active_count and active_count else None
        known_daily = sum(known_parts) if len(known_parts) == active_count and active_count else None
        # Consumables without recipe linkage have an explicit manual usage policy.
        if daily is None and number(policy.get('monthly_usage')) is not None:
            daily = number(policy['monthly_usage']) / month_days
        all_stock_known = bool(latest) and len(actual_parts) == len(latest) and not missing_places
        estimated = sum(actual_parts) if all_stock_known else None
        baseline = sum(baseline_parts) if baseline_parts else None
        if len(baseline_parts) != len(latest):
            issues.append('Итог ревизии неполный')
        if not raw.get('movement_coverage_confirmed'):
            issues.append('Полнота поступлений и развозов не подтверждена')
        if item in RECIPE_ITEMS:
            issues.append('Нормы по рабочей рецептуре; применимость дозировок нужно подтвердить')
        if daily is None:
            issues.append('Расход пока не определён')
        monthly = daily * month_days if daily is not None else None
        item_orders = [o for o in orders if o['item'] == item and o['status'] == 'ordered']
        in_transit = sum(o['quantity'] for o in item_orders)
        due_on, days_left, buy = None, None, None
        lead = number(policy.get('lead_days'))
        safety = number(policy.get('safety_days'))
        pack = number(policy.get('pack_size'))
        if estimated is not None and daily is not None and daily > 0:
            days_left = max(0, estimated / daily)
            covered = max(0, estimated)
            usable_orders = []
            for order in sorted(item_orders, key=lambda o: o['expected_on']):
                arrival = day(order.get('expected_on'))
                if arrival and today <= arrival <= today + timedelta(days=math.floor(covered / daily + 1e-9)):
                    covered += order['quantity']
                    usable_orders.append(order)
                elif arrival and arrival < today:
                    issues.append('Ожидаемый срок поставки прошёл: уточните получение')
                elif arrival:
                    issues.append('Заказ придёт после возможного исчерпания запаса')
            if lead is not None and safety is not None:
                due_on = (today + timedelta(days=math.floor(covered / daily - lead - safety + 1e-9))).isoformat()
            if pack and safety is not None:
                # For the current month buy only for remaining days. An order in transit
                # counts only if it arrives before a projected stockout and month end.
                start = max(today, target)
                end = target + timedelta(days=month_days)
                demand_days = max(0, (end - start).days)
                stock_at_start = estimated - daily * max(0, (start - today).days)
                usable_by_end = sum(o['quantity'] for o in usable_orders
                                    if day(o.get('expected_on')) < end)
                shortage = max(0, daily * (demand_days + safety) - stock_at_start - usable_by_end)
                buy = math.ceil((shortage - 1e-9) / pack) * pack if shortage else 0
        status = 'check'
        if due_on:
            status = 'urgent' if due_on <= today.isoformat() else ('soon' if due_on <= (today + timedelta(days=7)).isoformat() else 'ok')
        if lead is None or safety is None or not pack:
            issues.append('Укажите срок поставки, запас в днях и размер упаковки')
        rows.append({'name': item, 'unit': unit, 'revision': round(baseline, 4) if baseline is not None else None, 'estimated': round(estimated, 3) if estimated is not None else None,
                     'daily': round(daily, 4) if daily is not None else None,
                     'monthly': round(monthly, 2) if monthly is not None else None,
                     'known_daily': round(known_daily, 4) if known_daily is not None else None,
                     'known_monthly': round(known_daily * month_days, 2) if known_daily is not None else None,
                     'revision_complete': not missing_places and len(baseline_parts) == len(latest),
                     'buy': round(buy, 3) if buy is not None else None, 'in_transit': in_transit,
                     'days_left': round(days_left, 1) if days_left is not None else None,
                     'order_by': due_on, 'status': status, 'locations': locations,
                     'issues': list(dict.fromkeys(issues)), 'policy': policy})
    return {'generated_at': raw.get('fetched_at'), 'as_of': today.isoformat(), 'period': selected_period,
            'month': month, 'month_days': month_days, 'items': rows, 'orders': orders,
            'sources': raw.get('sources', []), 'warnings': warnings,
            'sales_window': raw.get('sales_window'),
            'pending_revisions': raw.get('pending_revisions', []),
            'service_reports': service_reports,
            'note': 'Расчёт до конца вчерашнего дня. Остатки ориентировочные; неучтённые перемещения и списания влияют на прогноз.'}
