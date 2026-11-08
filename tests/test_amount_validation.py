"""PR #5 amount regressions. Offline adapters; no build/sign on invalid quotes."""
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from mogit import gates
from mogit.ledger import LedgerError
from tests.test_gates import HAPPY, M, run
from tests.test_moneycore_gates import ledger, sess


@pytest.mark.parametrize('amount', [
    '0.02500000000000000000000000000000001', '0.02499999999999999999999999999999999',
    '0.0000000001', 'sNaN', 'NaN', 'Infinity', '-Infinity',
    '1e999999999', '-1e999999999', '1e-999999999',
])
def test_invalid_sol_refuses_before_build_or_sign(amount):
    m = M(sol=amount)
    se = sess(ledger()[0], m)
    m.build = se.vault.sign = lambda *a: pytest.fail('invalid quote reached build/sign')
    assert se.handle('/mogit').startswith('REFUSED')
    assert m.sent == []


@pytest.mark.parametrize('delta', ['-1e999999999', '-1e-999999999', 'sNaN', 'NaN', 'Infinity'])
def test_invalid_simulation_refuses_before_sign(delta):
    m = M(sim={'payer_lamports_delta': delta})
    se = sess(ledger()[0], m)
    se.vault.sign = lambda *a: pytest.fail('invalid simulation reached signer')
    assert run(se, *HAPPY).startswith('REFUSED')
    assert m.sent == []


def test_lamport_conversion_is_exact_even_with_low_context_precision():
    with localcontext() as ctx:
        ctx.prec = 3
        assert gates.check_quote(M(sol='0.025').q) == 25_000_000
        assert gates.check_quote(M(sol='0.02059164').q) == 20_591_640
        with pytest.raises(gates.GateError):
            gates.check_quote(M(sol='0.025000000000000000000000000001').q)


INVALID_USD = ['-1', '-0.01', 'sNaN', 'NaN', 'Infinity', '-Infinity', 'bad', '', None, True, {}, '1e999999999']


@pytest.mark.parametrize('usd', INVALID_USD)
@pytest.mark.parametrize('phase', ['start', 'render', 'confirm'])
def test_invalid_usd_flow_refuses_before_build_or_sign(usd, phase):
    m = M()
    se = sess(ledger()[0], m)
    if phase == 'render':
        run(se, *HAPPY[:-1])
    elif phase == 'confirm':
        assert 'CONFIRM CARD' in run(se, *HAPPY)
    m.q['usd'] = usd
    m.build = se.vault.sign = lambda *a: pytest.fail('invalid USD reached build/sign')
    assert se.handle({'start': '/mogit', 'render': 'none', 'confirm': 'LAUNCH'}[phase]).startswith('REFUSED')
    assert m.sent == []


def test_missing_usd_refuses():
    m = M()
    del m.q['usd']
    assert sess(ledger()[0], m).handle('/mogit').startswith('REFUSED')


@pytest.mark.parametrize('usd', INVALID_USD)
@pytest.mark.parametrize('method', ['check', 'reserve', 'commit'])
def test_ledger_rejects_invalid_cost_without_mutation(usd, method):
    L, path = ledger()
    before = Path(path).read_bytes()
    with pytest.raises(LedgerError):
        getattr(L, method)(usd, {}) if method != 'check' else L.check(usd)
    assert Path(path).read_bytes() == before


@pytest.mark.parametrize('usd', ['0', '30'])
def test_ledger_accepts_zero_and_exact_budget(usd):
    L, _ = ledger()
    L.check(usd)
    rid = L.reserve(usd, {})
    assert L.settle(rid, {}) == Decimal(usd)


@pytest.mark.parametrize('usd', ['-1', 'NaN', 'sNaN', 'Infinity', 'bad', None])
def test_corrupt_pending_cost_cannot_be_settled(usd):
    L, path = ledger(pending=[{'id': 'bad', 'cost_usd': usd}])
    before = Path(path).read_bytes()
    with pytest.raises(LedgerError):
        L.settle('bad', {})
    assert Path(path).read_bytes() == before
    assert L.kill_on() is True


def test_zero_quote_with_extreme_exponent_is_exact():
    assert gates.check_quote(M(sol='0e999999999999999999').q) == 0
