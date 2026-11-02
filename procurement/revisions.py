"""Read BDR inventory blocks without treating month labels as physical dates."""
import re
from datetime import date, timedelta
from .engine import ITEMS, number, day

BDR_ID = '1GZhau1YOqKKD_FUvdWrtHMkVmFscqmBoGejYWP-X3sU'
LOCATIONS = {'Сити':'Сити','Южн':'Южный','Южный':'Южный','Белом':'Беломорский','Беломорский':'Беломорский',
             'Бел 2':'Бел2','Гагарина':'Гагарина','Макси':'Макси','Гиппо':'Гиппо','Дома':'Дома','Гараж':'Гараж'}
ALIASES = {'Кофе':'Кофе','Молоко':'Молоко','Шоколад':'Шоколад','Мока':'Мока','Раф банан':'Раф банан',
           'Сахар':'Сахар','Сироп':'Сиропы','Стаканы':'Стаканы','КрышкиЧ':'Крышки чёрн','КрышкиБ':'Крышки бел',
           'Палочки':'Палочки','Трубочки':'Трубочки','Манжеты':'Манжеты','Капхолдеры':'Манжеты',
           'Салф влажн':'Влажные салф','Салф сухие':'Салфетки сухие','Пакеты':'Мус.пакеты'}


def merge_revisions(primary, grid):
    rows, current, columns = {}, None, {}
    issues = []
    for n, row in enumerate(grid, 1):
        label = str(row[1] or '').strip() if len(row) > 1 else ''
        if len(row) > 2 and row[2] == 'Цена' and isinstance(row[1], (int, float)):
            label = (date(1899, 12, 30) + timedelta(days=int(row[1]))).strftime('%d.%m.%Y')
        match = re.fullmatch(r'\d{1,2}\.(\d{1,2})\.(20\d{2})', label)
        if match:
            month, year = map(int, match.groups())
            if not 1 <= month <= 12:
                current = None
                continue
            current = f'{month:02d}.{year}'
            columns = {i:LOCATIONS[str(v).strip()] for i,v in enumerate(row) if str(v).strip() in LOCATIONS}
            for column, location in columns.items():
                key = (current, location)
                if key in rows:
                    raise ValueError('Несколько блоков БДР за один месяц')
                rows[key] = {'Период':current, 'Локация':location, 'Дата заполнения':'',
                             '_item_dates':{}, '_item_sources':{}, '_conflicts':{}, '_from_bdr':True,
                             '_bdr_label':label, '_bdr_row':n}
            if day(label) is None:
                issues.append(f'БДР: заголовок «{label}» — метка месяца с некорректным днём, не дата физического счёта')
            continue
        item = ALIASES.get(label)
        if current is None or not item:
            continue
        for col, location in columns.items():
            q = number(row[col]) if len(row) > col else None
            record = rows[current,location]
            record[item] = q
            record['_item_sources'][item] = f'БДР!{chr(65+col)}{n}'
    for record in primary:
        key = (record.get('Период'), record.get('Локация'))
        if key not in rows:
            rows[key] = dict(record)
            continue
        target = rows[key]
        physical = record.get('Дата заполнения', '')
        for item in ITEMS:
            bdr, source = number(target.get(item)), number(record.get(item))
            if source is None:
                continue
            if bdr is None:
                target[item] = source
                target['_item_dates'][item] = physical
                target['_item_sources'][item] = 'Первичная ревизия бота'
            elif abs(bdr-source) < 1e-8:
                target['_item_dates'][item] = physical
            elif item == 'Салфетки сухие' and abs(bdr * 500-source) < 1e-8:
                target[item] = source
                target['_item_dates'][item] = physical
                target['_item_sources'][item] += ' · пачки по 500 приведены к штукам по совпадающему первичному счёту'
            else:
                target['_conflicts'][item] = {'bdr':bdr, 'primary':source}
        # In BDR a month label cannot supply the missing physical count date.
    return list(rows.values()), issues


def pending_reports(grid):
    if not grid:
        return []
    result = []
    for n, row in enumerate(grid[1:],2):
        r = dict(zip(grid[0],row))
        if r.get('Статус') != 'saved' or r.get('Revision_Mode') != 'pending_bdr':
            continue
        counted = day(r.get('Дата'))
        if not counted:
            continue
        # Follow the existing bot month cutoff without altering the report date.
        from datetime import timedelta
        period_day = counted.replace(day=1)-timedelta(days=1) if counted.day <= 12 else counted
        chat = str(r.get('Chat_ID','')).removeprefix('-100')
        message = str(r.get('Source_Message_ID',''))
        result.append({'point':r.get('Точка'), 'date':counted.isoformat(), 'period':period_day.strftime('%m.%Y'),
                       'status':'Ожидает переноса в БДР', 'row':n,
                       'url':f'https://t.me/c/{chat}/{message}' if chat.isdigit() and message.isdigit() else None})
    return result
