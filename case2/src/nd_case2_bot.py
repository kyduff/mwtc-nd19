import argparse
import random

import sys
import py_vollib.black_scholes as bs
from py_vollib.black_scholes import implied_volatility
import datetime
import py_vollib.black_scholes.greeks.analytical as bsga
import py_vollib.black_scholes.greeks.numerical as bsgn
import multiprocessing as mp
import math
from client.exchange_service.client import BaseExchangeServerClient
from protos.order_book_pb2 import Order
from protos.service_pb2 import PlaceOrderResponse



# gets spread and base_price using primitive adjustment method
def _get_spread_scaled(bsp, wmp, ask, bid):
    x3 = ask - wmp
    x2 = wmp - bsp
    x1 = bsp - bid
    # print(bid, ask)
    ask = ask - (x2/(bid-ask))*x3
    bid = bid - (x1/(bid-ask))*x1
    return (ask - bid, (ask + bid)/2)


def get_qty(*args, method="discrep"):
    ''' arg:
            0: spread
            1: price discrepency
    '''
    if method == "discrep":
        # factors to determine scaling of qty with spread and price discrepency
        SPREAD_FACTOR = 500
        DISCREP_FACTOR = 0
        return args[0]*SPREAD_FACTOR + args[1]*DISCREP_FACTOR


# computes weighted price
def weighted_price(mid_price, imbalance, best_ask, best_bid):
    return imbalance*best_ask + (1-imbalance)*best_bid


class NDMarketMaker(BaseExchangeServerClient):


    def __init_asset_codes(self):
        asset_codes = {}
        for code in self.codes:
            asset_codes[code] = {}
            asset_codes[code]["strike"] = int(code[1:-3])
            asset_codes[code]["vol"] = 8.*abs(math.log(100./asset_codes[code]["strike"]))
            asset_codes[code]["price"] = bs.black_scholes(code[0].lower(), 100, asset_codes[code]["strike"],
                                                          0.25, 0, asset_codes[code]["vol"])
        return asset_codes

    
    def __init_inventory(self):
        inventory = {}
        for code in self.codes:
            inventory[code] = 0
        inventory[self.underlying_code] = 0
        return inventory


    def __init__(self, *args, **kwargs):
        self.START_TIME = datetime.datetime.now()
        BaseExchangeServerClient.__init__(self, *args, **kwargs)

        self.codes = ["C98PHX", "P98PHX",
                 "C99PHX", "P99PHX", 
                 "C100PHX", "P100PHX", 
                 "C101PHX", "P101PHX", 
                 "C102PHX", "P102PHX",]
        self._orderids = set([])
        self.asset_codes = self.__init_asset_codes()
        self.underlying_code = "IDX#PHX"
        self.underlying_price = 100
        self.inventory = self.__init_inventory()


    # more complicated than we thought
    # as of now, assumes assets are uncorrelated
    def _get_vega(self, code):
        return bsgn.vega(code[0].lower(), self.underlying_price, self.asset_codes[code]["strike"], self.get_time_to_exp(),0,self.asset_codes[code]["vol"])


    def _get_delta(self, code):
        return bsgn.delta(code[0].lower(), self.underlying_price, self.asset_codes[code]["strike"], self.get_time_to_exp(),0, self.asset_codes[code]["vol"])


    # consolidate with _get_position_delta to avoid redundancy
    def _get_position_vega(self):
        vega = 0.
        net_position = sum(list(self.inventory.values()))
        if not net_position: return 0
        for code in self.codes:
            # vega += (self.inventory[code]/net_position)*self._get_vega(code)
            vega += self.inventory[code]*self._get_vega(code)
        return vega 


    def _get_position_delta(self):
        delta = 0.
        net_position = sum(list(self.inventory.values()))
        if not net_position: return 0
        for code in self.codes:
            # delta += (self.inventory[code]/net_position)*self._get_delta(code)
            delta += self.inventory[code]*self._get_delta(code)
        return delta 

    def _get_portfolio_delta(self):
        return self._get_position_delta() + self.inventory[self.underlying_code]


    def _make_order(self, asset_code, quantity, base_price, spread, bid=True):
        return Order(asset_code = asset_code, quantity= int(quantity if bid else -1*quantity),
                     order_type = Order.ORDER_LMT,
                     price = round(base_price-spread/2 if bid else base_price+spread/2, 2),
                     competitor_identifier = self._comp_id)


    def _make_mkt_order(self, asset_code, quantity):
        return Order(asset_code = asset_code, quantity=int(quantity),
                     order_type = Order.ORDER_MKT,
                     competitor_identifier = self._comp_id)


    # computes time to expiration
    def get_time_to_exp(self):
        return (3-((datetime.datetime.now()-self.START_TIME).total_seconds()*1./900.))*(1./12.)


    # hedges delta of a single option
    def hedge_delta(self, fill):
        code = fill.order.asset_code
        qty = fill.filled_quantity
        delta = self._get_delta(code)
        if qty*delta == 0: return
        order_resp = self._make_mkt_order(self.underlying_code, qty*delta if code[0].lower() == 'p' else -qty*delta) # buy(sell) delta underlying for each option exchanged
        if type(order_resp) != PlaceOrderResponse:
            pass
            # print(3, order_resp)
        else:
            self._orderids.add(order_resp.order_id)

    # hedges delta of whole portfolio
    def rebalance_delta(self):
        # print("Rebalancing delta...")
        #for code in self.codes:
        #    qty = self.inventory[code]
        #    delta = self._get_delta(code)
        #    order_resp = self._make_mkt_order(self.underlying_code, qty*delta if code[0].lower() == 'p' else -qty*delta) # buy(sell) delta underlying for each option exchanged
        option_delta = self._get_position_delta()
        order_resp = self._make_mkt_order(self.underlying_code, -option_delta) # buy(sell) delta underlying for each option exchanged
        if type(order_resp) != PlaceOrderResponse:
            pass
            # print(-option_delta, order_resp)
        else:
            self._orderids.add(order_resp.order_id)

    # WIP
    def rebalance_delta_option(self, code):
        this_delta = self._get_delta(code)
        print(this_delta)
        this_qty = self.inventory[code]
        num_underlying = self.inventory[self.underlying_code]
        other_delta = self._get_position_delta() - this_delta*this_qty
        order_qty = ((-num_underlying - other_delta)/this_delta) - this_qty
        order_resp = self._make_mkt_order(code, order_qty)
        if type(order_resp) != PlaceOrderResponse:
            pass
            # print(-option_delta, order_resp)
        else:
            self._orderids.add(order_resp.order_id)



    # Review
    def hedge_vega(self, fill):
        code = fill.order.asset_code
        qty = fill.filled_quantity
        vegaP = self._get_position_vega()
        vega = self._get_vega(code)
        hedge_qty = abs(qty)*(-vegaP)/vega
        if hedge_qty == 0: return
        order_resp = self._make_mkt_order(code, hedge_qty) # buy(sell) -vega/vegaT of each option exchanged
        if type(order_resp) != PlaceOrderResponse:
            pass
            # print(4, order_resp)
        else:
            self._orderids.add(order_resp.order_id)

    # hedges vega of whole portfolio
    def rebalance_vega(self, vegaP):
        # print("Rebalancing vega...")
        for code in self.codes:
            qty = self.inventory[code]
            vega = self._get_vega(code)
            hedge_qty = abs(qty)*(-vegaP)/vega
            order_resp = self._make_mkt_order(code, hedge_qty) # buy(sell) -vega/vegaT underlying for each option exchanged
            if type(order_resp) != PlaceOrderResponse:
                pass
                # print(4, order_resp)
            else:
                self._orderids.add(order_resp.order_id)


    # In development
    def get_spread(self, *args, method="scaled"):
        ''' Usage:
            Method:
                stoikov
                    - arg0: volatility
                    - arg1: base price
        '''
        if method == "stoikov":
            return (args[0]*(self.get_time_to_exp()) + math.log(2), args[1])
        elif method == "scaled":
            return _get_spread_scaled(args[0],args[1],args[2],args[3])


    # gets meaured price, designed so we can easily adjust it
    def get_measured_price(self, *args, method="weighted"):
        if method == "weighted":
            return weighted_price(args[0], args[1], args[2], args[3])


    # Sends orders to exchange
    def send_order(self, asset_code, qty, base_price, spread, kind="lmt"):
        qty = int(qty)
        if kind == "lmt":
            base_price = round(base_price, 2)
            ask_resp = self.place_order(self._make_order(asset_code, qty, base_price, spread,  False))
            bid_resp = self.place_order(self._make_order(asset_code, qty, base_price, spread,  True))
    
            if type(ask_resp) != PlaceOrderResponse:
                print(1, ask_resp)
            else:
                self._orderids.add(ask_resp.order_id)
            
            if type(bid_resp) != PlaceOrderResponse:
                print(2, bid_resp)
            else:
                self._orderids.add(bid_resp.order_id)



    # Generates then sends orders
    MIN_SPREAD = 0.02
    def generate_limit_order(self, asset_code, measured_price, volatility, best_ask, best_bid, min_spread=MIN_SPREAD):
        bs_price = bs.black_scholes(asset_code[0].lower(), self.underlying_price, self.asset_codes[asset_code]["strike"], self.get_time_to_exp(), 0, volatility)
        spread, base_price = self.get_spread(bs_price, measured_price, best_ask, best_bid, method="scaled")

        if spread < min_spread: return

        self.send_order(asset_code, get_qty(spread, abs(base_price - bs_price)), base_price, spread)
        self.asset_codes[asset_code]["vol"] = bs.implied_volatility.implied_volatility(measured_price, self.underlying_price, self.asset_codes[asset_code]["strike"],
                                                                                                    self.get_time_to_exp(), 0, asset_code[0].lower())


    def handle_exchange_update(self, exchange_update_response):
        ''' Method for handling exchange updates
            - gathers and exchange update and responds
            - creates a process for each symbol
            --> (not robust, may not work, haven't experimented with
                memory sharing/dict access)
        '''
        #
        # TODO: Figure out how to get price of the underlying
        #
        # print(len(exchange_update_response.market_updates))
        # for update in exchange_update_response.market_updates:
        #     print(update.asset.asset_code, ':', len(update.bids))
        print("pnl:", exchange_update_response.competitor_metadata.pnl)
        deltaP = self._get_portfolio_delta() 
        vegaP = self._get_position_vega()
        print("delta:", deltaP)
        print("vega:", vegaP)
        print("num_underlying:", self.inventory[self.underlying_code])
        updates = {}
        for update in exchange_update_response.market_updates:
            updates[update.asset.asset_code] = update
            # print(update.asset.asset_code)

        try:
            self.underlying_price = updates.get(self.underlying_code, 0).mid_market_price
        except AttributeError:
            pass
        print("price:", self.underlying_price)
  
        # processes = []
        code_min_spread = self.codes[0]
        min_spread = sys.maxsize
        for code in list(self.asset_codes.keys()):
            update = updates.get(code, 0)

            if not update or not update.bids or not update.asks:
                measured_price = self.asset_codes[code]["price"]
                init_spread = 0.5
                self.generate_limit_order(code, measured_price, self.asset_codes[code]["vol"], measured_price+init_spread/2, measured_price-init_spread/2, 0.02)
        #       p = mp.Process(target=self.generate_limit_order, args=(code, measured_price, self.asset_codes[code]["vol"], measured_price+init_spread/2, measured_price-init_spread/2, 0.02))
            else:
                spread = update.asks[0].price - update.bids[0].price
                if spread < min_spread:
                    min_spread = spread
                    code_min_spread = code
                imbalance = update.bids[0].size / (update.bids[0].size + update.asks[0].size)
                measured_price = self.get_measured_price(update.mid_market_price, imbalance, update.asks[0].price, update.bids[0].price)
                self.generate_limit_order(code, measured_price, self.asset_codes[code]["vol"], update.asks[0].price, update.bids[0].price, 0.02)

        # Do we want to process fills before or after processing market move?
        if exchange_update_response.fills:
            for fill in exchange_update_response.fills:
                self.inventory[fill.order.asset_code] += fill.filled_quantity
                # self.hedge_delta(fill)
                # self.hedge_vega(fill)
                # deltaP = self._get_position_delta() 
                # vegaP = self._get_position_vega()
                self.rebalance_delta()
                    # self.rebalance_delta_option(code_min_spread)
                self.rebalance_vega(vegaP)

        #   TODO: multiprocessing
        #        p = mp.Process(target=self.generate_limit_order, args=(code, measured_price, self.asset_codes[code]["vol"], update.ask, update.bid, 0.02))

        #    processes.append(p)
        #    p.start()

        #for p in processes:
        #    p.join()



    def tester(self):
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run the exchange client')
    parser.add_argument("--server_host", type=str, default="localhost")
    parser.add_argument("--server_port", type=str, default="50052")
    parser.add_argument("--client_id", type=str)
    parser.add_argument("--client_private_key", type=str)
    parser.add_argument("--websocket_port", type=int, default=5678)
    
    args = parser.parse_args()
    host, port, client_id, client_pk, websocket_port = (args.server_host, args.server_port,
                                                        args.client_id, args.client_private_key,
                                                        args.websocket_port)
    
    client = NDMarketMaker(host, port, client_id, client_pk, websocket_port)
    client.start_updates()
