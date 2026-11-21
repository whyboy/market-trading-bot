"""纯内存模拟调仓；账户持久化由 AccountLedger 负责。"""
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from market_trading_bot.strategy.StrategyData import StrategyData


class PapTrader:
    """按整手、可卖数量、佣金和现金约束模拟成交，先卖后买。"""

    def __init__(self, settings: dict, instruments: dict):
        """读取费用与标的规则；费用非负，滑点须小于 1。"""
        self.commission_rate = Decimal(str(settings['commission_rate']))
        self.min_commission = Decimal(str(settings['minimum_commission']))
        self.slippage = Decimal(str(settings['slippage_rate']))
        if any(not value.is_finite() or value < 0 for value in (self.commission_rate, self.min_commission, self.slippage)) or self.slippage >= 1:
            raise ValueError('费用必须为非负有限数，滑点必须小于1')
        for rule in instruments.values():
            if type(rule['lot_size']) is not int or rule['lot_size'] <= 0 or type(rule['t_plus_one']) is not bool:
                raise ValueError('交易单位必须为正整数，t_plus_one必须为布尔值')
        self.instruments = instruments

    def rebalance(self, signal: StrategyData, account: dict) -> dict:
        """使用策略生成的目标权重，按调仓前净资产计算份额；先卖后买，返回成交及新账户。"""
        day = signal.as_of.date().isoformat()
        if account['date'] and day <= account['date']:
            raise ValueError('调仓日期必须晚于上次记账日期')
        cash = Decimal(account['cash'])
        positions = {symbol: dict(position) for symbol, position in account['positions'].items()}
        prices = {item.symbol: Decimal(str(item.price)) for item in signal.ranking}
        weights = signal.target_weights
        if not positions.keys() <= prices.keys():
            raise ValueError('行情必须覆盖所有持仓标的')
        for position in positions.values():
            position['sellable'] = position['quantity']
        equity = cash + sum(prices[symbol] * position['quantity'] for symbol, position in positions.items())
        targets = {}
        for symbol, weight in weights.items():
            lot = self.instruments[symbol]['lot_size']
            targets[symbol] = int(equity * Decimal(str(weight)) / prices[symbol] / lot) * lot
        orders, fills = [], []
        for side in ('sell', 'buy'):
            for symbol, target in targets.items():
                position = positions.setdefault(symbol, {'quantity': 0, 'sellable': 0})
                delta = target - position['quantity']
                if (side == 'buy' and delta <= 0) or (side == 'sell' and delta >= 0):
                    continue
                requested = abs(delta)
                lot = self.instruments[symbol]['lot_size']
                price = (prices[symbol] * (1 + self.slippage if side == 'buy' else 1 - self.slippage)).quantize(
                    Decimal('.001'), rounding=ROUND_CEILING if side == 'buy' else ROUND_FLOOR)
                if price <= 0:
                    raise ValueError('滑点后价格必须为正数')
                quantity = min(requested, position['sellable']) if side == 'sell' else requested
                if side == 'buy':
                    # 按实际取整后的佣金判断，避免临界现金少买一手。
                    low, high = 0, quantity // lot
                    while low < high:
                        middle = (low + high + 1) // 2
                        amount = self.money(price * middle * lot)
                        if amount + self.commission(amount) <= cash:
                            low = middle
                        else:
                            high = middle - 1
                    quantity = low * lot
                amount = self.money(price * quantity)
                fee = self.commission(amount) if quantity else Decimal('0.00')
                if side == 'sell' and quantity and cash + amount < fee:
                    quantity, amount, fee = 0, Decimal('0.00'), Decimal('0.00')
                order = {'order_id': f'{day}-{len(orders) + 1}', 'symbol': symbol, 'side': side,
                         'requested_quantity': requested, 'filled_quantity': quantity,
                         'status': 'filled' if quantity == requested else 'partial' if quantity else 'rejected',
                         'reason': '' if quantity == requested else 'cash_limit' if side == 'buy' or not quantity else 'sellable_limit'}
                orders.append(order)
                if not quantity:
                    continue
                cash += amount - fee if side == 'sell' else -amount - fee
                position['quantity'] += quantity if side == 'buy' else -quantity
                if side == 'sell':
                    position['sellable'] -= quantity
                elif not self.instruments[symbol]['t_plus_one']:
                    position['sellable'] += quantity
                fills.append({'fill_id': order['order_id'], 'order_id': order['order_id'],
                              'symbol': symbol, 'side': side, 'quantity': quantity,
                              'price': str(price), 'amount': str(amount), 'commission': str(fee)})
        positions = {symbol: position for symbol, position in positions.items() if position['quantity']}
        equity = self.money(cash + sum(prices[symbol] * position['quantity'] for symbol, position in positions.items()))
        return {'date': day, 'cash': str(self.money(cash)), 'positions': positions,
                'orders': orders, 'fills': fills, 'equity': str(equity)}

    def commission(self, amount: Decimal) -> Decimal:
        """取比例佣金与最低佣金的较大值，再按分四舍五入。"""
        return self.money(max(amount * self.commission_rate, self.min_commission))

    @staticmethod
    def money(value: Decimal) -> Decimal:
        """金额按分四舍五入。"""
        return value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
