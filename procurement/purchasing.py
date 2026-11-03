"""Observed delivery dates from the existing purchasing book, not assumed lead times."""
from datetime import date, datetime, timedelta
from .engine import number


def sheet_date(value, year):
    if isinstance(value,(int,float)) and not isinstance(value,bool) and 30000 <= value < 100000:
        return date(1899,12,30)+timedelta(days=int(value))
    if not value:
        return None
    for fmt in ('%d.%m.%Y','%Y-%m-%d','%d.%m'):
        try:
            result=datetime.strptime(str(value).strip(),fmt).date()
            return result.replace(year=year) if fmt=='%d.%m' else result
        except ValueError:
            pass
    return None


def purchase_history(rows, year):
    history=[]
    for n,row in enumerate(rows,1):
        if len(row)<3:
            continue
        ordered=sheet_date(row[1],year)
        quantity=number(row[2])
        if not ordered or quantity is None:
            continue
        received=sheet_date(row[8],year) if len(row)>8 else None
        supplier=str(row[7] or '').strip() if len(row)>7 else ''
        name=str(row[0] or 'Позиция товарного блока').strip()
        match=next((item for part,item in [('шоколад','Шоколад'),('сахар','Сахар'),('стакан','Стаканы')]
                    if part in name.casefold()),None)
        history.append({'name':name,'item':match,'quantity':quantity,'supplier':supplier,
                        'ordered_on':ordered.isoformat(),'received_on':received.isoformat() if received else None,
                        'delivery_days':(received-ordered).days if received and received>=ordered else None,
                        'source_row':n})
    by_supplier={}
    for item in history:
        if item['delivery_days'] is None or not item['supplier']:
            continue
        key=(item['supplier'],item['ordered_on'],item['received_on'])
        by_supplier[key]=item['delivery_days']
    summaries=[]
    for supplier in sorted({k[0] for k in by_supplier}):
        days=[v for k,v in by_supplier.items() if k[0]==supplier]
        summaries.append({'supplier':supplier,'shipments':len(days),'min_days':min(days),'max_days':max(days)})
    return history,summaries
