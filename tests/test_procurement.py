import hashlib
import hmac
import json
from datetime import date
from urllib.parse import urlencode

import pytest

from procurement.app import validate_init_data
from procurement.engine import build_dashboard, consumption, recipe_index, latest_service_reports
from procurement.store import Store


def recipe():
    return ['Южный','Капучино','Китайский',1,17,0,0,305,'сек',10,47.6,0,0]


def test_unknown_recipe_does_not_create_zero_ingredient_usage():
    values, missing = consumption({'point':'Южный','products':[{'name':'Неизвестный напиток','sales_count':4}]},recipe_index([recipe()]))
    assert values['Кофе'] is None
    assert values['Стаканы'] == 4
    assert missing == ['Неизвестный напиток']


def test_final_costing_drives_usage_and_owner_espresso_dose():
    final = [['Южный', 'Банановый коктейль', 0, 32.48, 0, 22.32]]
    recipes = recipe_index([], final)
    amounts, missing = consumption({'point': 'Южный', 'products': [
        {'name': 'Банановый коктейль', 'sales_count': 2},
        {'name': 'Эспрессо', 'sales_count': 1}]}, recipes)
    assert missing == []
    assert amounts['Кофе'] == 0.01
    assert amounts['Молоко'] == pytest.approx(0.06496)
    assert amounts['Раф банан'] == pytest.approx(0.04464)


def test_final_costing_discrepancy_is_not_silently_ignored():
    with pytest.raises(ValueError, match='расходится'):
        recipe_index([recipe()], [['Южный', 'Капучино', 30, 47.6, 0, 0]])


def test_service_journal_shows_latest_visit_without_inventing_stock_count():
    rows = [
        {'Дата': '02.11.2026', 'Точка': 'Макси', 'Вода(бут)': '3', '_row': 461},
        {'Дата': '31.10.2026', 'Точка': 'Макси', 'Вода(бут)': '3,2', '_row': 457},
    ]
    assert latest_service_reports(rows, ['Макси']) == [
        {'point': 'Макси', 'date': '2026-11-02', 'water': 3.0,
         'shortage': '', 'stock_note': '', 'source_row': 461}]


def test_fresh_count_updates_estimate_but_preserves_monthly_revision():
    r = raw()
    r['manual_counts'] = [{'item': 'Кофе', 'location': 'Южный',
                           'quantity': 0.5, 'counted_on': '2026-11-01', 'note': 'Проверено'}]
    coffee = build_dashboard(r, {}, [], today=date(2026, 11, 1))['items'][0]
    assert coffee['revision'] == 2
    assert coffee['locations'][0]['manual_count'] == 0.5
    assert coffee['estimated'] == 0.5


def test_count_after_sales_requires_dated_transactions():
    r = raw()
    r['manual_counts'] = [{'item': 'Кофе', 'location': 'Южный',
                           'quantity': 0.5, 'counted_on': '2026-10-30', 'note': ''}]
    r['sales'] = [{'point': 'Южный', 'since': '2026-10-05', 'until': '2026-11-01',
                   'products': [{'name': 'Эспрессо', 'sales_count': 1}],
                   'daily_complete': True, 'daily_products': {'2026-11-01': {'Эспрессо': 1}}}]
    coffee = build_dashboard(r, {}, [], today=date(2026, 11, 2))['items'][0]
    assert coffee['estimated'] == pytest.approx(0.49)
    del r['sales'][0]['daily_complete']
    assert build_dashboard(r, {}, [], today=date(2026, 11, 2))['items'][0]['estimated'] is None


def test_operational_count_is_durable_and_retry_safe(tmp_path):
    store = Store(tmp_path / 'operational.sqlite3')
    payload = {'id': 'f4795d3d-35c3-435c-831b-fd56af09cf1b', 'item': 'Кофе',
               'location': 'Дома', 'quantity': 0, 'counted_on': '2026-10-31', 'note': 'Подсчёт'}
    store.count(payload, 1)
    store.count(payload, 1)
    assert len(store.counts()) == 1
    assert store.counts()[0]['quantity'] == 0
    with pytest.raises(ValueError):
        store.count({**payload, 'quantity': 2}, 1)


def raw():
    return {'active_points':['Южный'],'recipes':[recipe()], 'movement_coverage_confirmed':True,
            'revisions':[{'Период':'10.2026','Локация':'Южный','Дата заполнения':'30.10.2026','Кофе':'2','Молоко':'3','Мока':'0','Шоколад':'0','Стаканы':'300'},
                         {'Период':'10.2026','Локация':'Дома','Дата заполнения':'30.10.2026','Кофе':'0','Молоко':'0','Мока':'0','Шоколад':'0','Стаканы':'0'}],
            'sales':[{'point':'Южный','since':'2026-10-04','until':'2026-10-31','products':[{'name':'Капучино','sales_count':280}]}],
            'since_revision_sales':[{'point':'Южный','since':'2026-10-31','until':'2026-10-31','products':[{'name':'Капучино','sales_count':10}]}]}


def test_revision_date_not_accounting_month_drives_consumption():
    data=build_dashboard(raw(),{'Кофе':{'lead_days':3,'safety_days':2,'pack_size':1}},[],today=date(2026,11,1))
    coffee=data['items'][0]
    assert coffee['revision']==2
    assert coffee['estimated']==1.9
    assert coffee['daily']==0.1
    assert coffee['monthly']==3
    assert coffee['buy']==2
    assert coffee['order_by']=='2026-11-15'


def test_missing_point_or_date_is_unknown_not_zero():
    r=raw();r['active_points'].append('Гагарина')
    coffee=build_dashboard(r,{},[],today=date(2026,11,1))['items'][0]
    assert coffee['daily'] is None and coffee['estimated'] is None and coffee['buy'] is None


def test_same_day_delivery_is_not_silently_added():
    r=raw();r['movements']=[{'item':'Кофе','date':'30.10.2026','point':'Южный','source':'Дома','quantity':2}]
    coffee=build_dashboard(r,{},[],today=date(2026,11,1))['items'][0]
    assert coffee['estimated'] is None


def test_late_supply_cannot_prevent_prior_stockout():
    p={'Кофе':{'lead_days':3,'safety_days':2,'pack_size':1}}
    o=[{'item':'Кофе','status':'ordered','quantity':10,'expected_on':'2026-12-01'}]
    coffee=build_dashboard(raw(),p,o,today=date(2026,11,1))['items'][0]
    assert coffee['order_by']=='2026-11-15' and coffee['buy']==2


def test_current_month_purchase_uses_remaining_days():
    r = raw()
    r['manual_counts'] = [{'item': 'Кофе', 'location': 'Южный',
                           'quantity': 0.5, 'counted_on': '2026-11-20', 'note': 'Проверено'}]
    coffee = build_dashboard(r, {'Кофе': {'lead_days': 3, 'safety_days': 2, 'pack_size': 1}},
                             [], today=date(2026, 11, 20))['items'][0]
    assert coffee['monthly'] == 3  # Full month still shown for planning.
    assert coffee['buy'] == 1  # 11 remaining days, not all 30 again.


def test_only_deliveries_before_stockout_reduce_monthly_purchase():
    p = {'Кофе': {'lead_days': 3, 'safety_days': 2, 'pack_size': 1}}
    on_time = [{'item': 'Кофе', 'status': 'ordered', 'quantity': 1, 'expected_on': '2026-11-10'}]
    late = [{'item': 'Кофе', 'status': 'ordered', 'quantity': 1, 'expected_on': '2026-12-01'}]
    early = build_dashboard(raw(), p, on_time, today=date(2026, 11, 1))['items'][0]
    delayed = build_dashboard(raw(), p, late, today=date(2026, 11, 1))['items'][0]
    assert early['buy'] == 1
    assert delayed['buy'] == 2


def signed(token,uid=123,stamp=1000):
    d={'auth_date':str(stamp),'user':json.dumps({'id':uid})}
    secret=hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    d['hash']=hmac.new(secret,'\n'.join(f'{k}={v}' for k,v in sorted(d.items())).encode(),hashlib.sha256).hexdigest()
    return urlencode(d)


def test_telegram_identity_allowlist_expiry_and_tampering():
    assert validate_init_data(signed('token'),'token',{123},now=1100)==123
    for value,owners,now in [(signed('wrong'),{123},1100),(signed('token'),{999},1100),(signed('token'),{123},5000)]:
        with pytest.raises(PermissionError):validate_init_data(value,'token',owners,now=now)


def test_procurement_access_does_not_inherit_payout_editor_role(monkeypatch):
    import bot
    monkeypatch.setattr(bot, 'PROCUREMENT_OWNER_IDS', '123')
    assert bot.get_procurement_owner_ids() == {123}
    monkeypatch.setattr(bot, 'PROCUREMENT_OWNER_IDS', '')
    with pytest.raises(RuntimeError, match='PROCUREMENT_OWNER_IDS'):
        bot.get_procurement_owner_ids()


def test_operational_order_retries_cannot_duplicate_or_change_existing(tmp_path):
    s=Store(tmp_path/'state.sqlite3');o={'id':'b2be6a9c-0482-4573-b0c1-733cfd60763d','item':'Кофе','quantity':5,'expected_on':'2026-11-05','supplier':'Поставщик'}
    s.order(o,123);s.order(o,123)
    assert len(s.orders())==1
    with pytest.raises(ValueError):s.order({**o,'quantity':6},123)
    assert not s.notifications()['enabled']


def test_received_order_counts_once_and_ambiguous_external_copy_stops_estimate():
    r=raw();o=[{'id':'local-receipt','item':'Кофе','status':'received','quantity':2,
                'received_on':'2026-10-31','location':'Дома'}]
    item=build_dashboard(r,{},o,today=date(2026,11,1))['items'][0]
    assert item['estimated']==3.9
    r['movements']=[{'item':'Кофе','date':'2026-10-31','point':'Дома','source':'Поставщик','quantity':2}]
    assert build_dashboard(r,{},o,today=date(2026,11,1))['items'][0]['estimated'] is None


def test_partial_monthly_usage_is_explicit_lower_bound():
    r=raw();r['sales'][0]['products'].append({'name':'item','sales_count':28})
    item=build_dashboard(r,{},[],today=date(2026,11,1))['items'][0]
    assert item['monthly'] is None and item['known_monthly']==3
    assert item['buy'] is None


def test_receiving_order_is_idempotent_and_cancel_is_not_a_receipt(tmp_path):
    s=Store(tmp_path/'state.sqlite3');o={'id':'b2be6a9c-0482-4573-b0c1-733cfd60763d','item':'Кофе','quantity':5,'expected_on':'2026-11-05','supplier':'Поставщик'}
    s.order(o,123)
    receipt={'id':o['id'],'received_on':'2026-10-31','location':'Дома'}
    s.receive(receipt);s.receive(receipt)
    assert s.orders()[0]['status']=='received'
    with pytest.raises(ValueError):s.cancel(o['id'])


def test_http_owner_auth_and_save_readback(tmp_path):
    import time
    import urllib.error
    import urllib.request
    from payroll_api import PayrollApiServer, PayrollApiConfig
    from procurement.app import Workspace
    class Sources:
        cached_at=time.monotonic()
    token='t'*40
    w=Workspace(Sources(),Store(tmp_path/'state.sqlite3'),token,lambda:{123})
    w.raw=raw()
    server=PayrollApiServer(PayrollApiConfig(token='p'*40,port=0,host='127.0.0.1'),payload_builder=lambda x:{},extra_routes=w)
    server.start()
    url=f'http://127.0.0.1:{server._server.server_port}/api/procurement'
    def request(data,auth=''):
        return urllib.request.urlopen(urllib.request.Request(url,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-Telegram-Init-Data':auth}),timeout=3)
    try:
        with pytest.raises(urllib.error.HTTPError) as err:request({'action':'snapshot'})
        assert err.value.code==403
        auth=signed(token,stamp=int(time.time()))
        with request({'action':'policy','item':'Кофе','lead_days':7,'safety_days':3,'pack_size':1,'monthly_usage':None},auth) as response:
            assert json.load(response)['ok']
        with request({'action':'snapshot'},auth) as response:
            assert json.load(response)['items'][0]['policy']['lead_days']==7
        w.preview=True
        with pytest.raises(urllib.error.HTTPError) as err:request({'action':'notifications','enabled':True})
        assert err.value.code==403
    finally:
        server.close()


def test_bdr_home_stock_and_invalid_month_label_do_not_invent_count_date():
    from procurement.revisions import merge_revisions
    grid=[[None,'31.09.2026','Цена','Южн','Дома','Гараж'],
          [None,'Кофе',1819,5.2,46,None], [None,'Раф банан',560,None,48,None],
          [None,'Мока',560,2.9,20,None]]
    primary=[{'Период':'09.2026','Локация':'Южный','Дата заполнения':'09.10.2026','Кофе':'5,2','Мока':'2,9'}]
    rows,issues=merge_revisions(primary,grid)
    home=next(r for r in rows if r['Локация']=='Дома')
    point=next(r for r in rows if r['Локация']=='Южный')
    assert home['Кофе']==46 and home['Раф банан']==48 and home['Мока']==20
    assert not home['_item_dates'] and point['_item_dates']['Кофе']=='09.10.2026'
    assert issues and '31.09.2026' in issues[0]


def test_bdr_primary_conflict_remains_visible():
    from procurement.revisions import merge_revisions
    rows,_=merge_revisions([{'Период':'09.2026','Локация':'Гагарина','Дата заполнения':'07.10.2026','Крышки бел':'450'}],
                           [[None,'31.09.2026','Цена','Гагарина'],[None,'КрышкиБ',1.9,600]])
    assert rows[0]['Крышки бел']==600
    assert rows[0]['_conflicts']['Крышки бел']=={'bdr':600,'primary':450}
    assert 'Крышки бел' not in rows[0]['_item_dates']


def test_raf_recipe_cannot_consume_moka_stock():
    values,_=consumption({'point':'Южный','products':[{'name':'Капучино','sales_count':1}]},recipe_index([recipe()]))
    assert 'Раф банан' in values and 'Мока' not in values


def test_pending_october_reports_are_visible_before_bdr_transfer():
    from procurement.revisions import pending_reports
    rows=[['Chat_ID','Source_Message_ID','Точка','Дата','Revision_Mode','Статус'],
          ['-1004430712013','354','Южный','31.10.2026','pending_bdr','saved']]
    r=pending_reports(rows)
    assert r[0]['period']=='10.2026' and r[0]['point']=='Южный'
    assert r[0]['url'].endswith('/4430712013/354')


def test_delivery_history_reads_native_sheet_dates_and_deduplicates_same_shipment():
    from procurement.purchasing import purchase_history
    rows=[['Шоколад',46302,20,731,14610,0,14610,'OZON',46309],
          ['Сахар',46312,2,2279,4558,0,4558,'OZON',46323],
          ['Стаканы',46312,1,100,100,0,100,'OZON',46323]]
    history,stats=purchase_history(rows,2026)
    assert history[0]['ordered_on']=='2026-10-07'
    assert history[0]['received_on']=='2026-10-14'
    assert stats==[{'supplier':'OZON','shipments':2,'min_days':7,'max_days':11}]
