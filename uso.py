# uso.py - BTC 15m MONITOR + INFO EXTENDIDA (CORREGIDO - ENERO 2026)
# Modificado para integrar con MT4 via CSV: Detecta señales de "Sinal.csv" y ejecuta trades automáticos en Polymarket.

import requests
import json
import time
from datetime import datetime, timezone, timedelta
import pytz
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, 
    MarketOrderArgs, 
    OrderType, 
    OpenOrderParams, 
    BalanceAllowanceParams, 
    AssetType
)
from py_clob_client.order_builder.constants import BUY, SELL
import csv
import os

# ==================== CONFIG ====================
class Config:
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    CHAIN_ID = 137
    
    PRIVATE_KEY = "0xec151efd6f10e1de1d3ba58adc2b92a2133e24484a92e12a5d99145c9e3ef834"
    FUNDER_ADDRESS = "0x3576B9f96046171012A33F59aa7349e36a26270D"
    SIGNATURE_TYPE = 1
    
    AUTO_SWITCH_ENABLED = True
    MONITOR_INTERVAL_SEC = 5
    CACHE_REFRESH_SEC = 60  # ✅ AGREGADO: Faltaba esta constante
    SERIES_PATTERN = "btc-updown-15m-"
    LOOKBACK_HOURS = 2
    LOOKAHEAD_HOURS = 1.5
    
    # Nueva config para integración con MT4
    CSV_PATH = r"C:\Program Files (x86)\MetaTrader 4\MQL4\Files\Sinal.csv"  # ¡Cambia esto al path real del CSV de MT4!
    LAST_TIMESTAMP = 0  # Global para rastrear la última señal procesada (inicializa en 0)

# ==================== CLASE ====================
class PolymarketTrader:
    def __init__(self):
        self.read_client = ClobClient(Config.CLOB_API)
        self.auth_client = None
        self.selected_market = None
        self.selected_token_ids = None
        self.cache = []
        self.cache_time = 0
        self.upcoming = []
        self.trade_amount = 1.0  # Monto predeterminado para trades automáticos
        self.authenticate()
    
    def authenticate(self):
        try:
            print("\n🔐 Autenticando...")
            self.auth_client = ClobClient(
                Config.CLOB_API, 
                key=Config.PRIVATE_KEY, 
                chain_id=Config.CHAIN_ID, 
                signature_type=Config.SIGNATURE_TYPE, 
                funder=Config.FUNDER_ADDRESS
            )
            creds = self.auth_client.derive_api_key()
            self.auth_client.set_api_creds(creds)
            print("✅ OK!")
            bal = self.get_balance()
            print(f"   Balance: ${bal:,.2f} USDC" if bal else "   Balance no disponible")
        except Exception as e:
            print(f"❌ Auth error: {e}")
    
    def get_balance(self):
        """Obtiene balance USDC de la wallet"""
        if not self.auth_client: 
            return None
        try:
            b = self.auth_client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            return int(b['balance']) / 1e6
        except:
            return None
    
    def show_balance(self):
        """Muestra balance formateado"""
        bal = self.get_balance()
        if bal is not None:
            print(f"\n💰 Balance: ${bal:,.2f} USDC")
        else:
            print("\n❌ No se pudo obtener balance")
    
    def generate_timestamps(self):
        """
        Genera timestamps para mercados BTC 15m
        - Intervalo: 900 segundos (15 minutos)
        - Rango: 2 horas atrás + 1.5 horas adelante
        - Retorna timestamps redondeados a intervalos de 15 min
        """
        now_utc = datetime.now(timezone.utc)
        interval_sec = 900  # 15 minutos
        timestamps = []
        
        # Timestamps pasados (2 horas = 8 intervalos)
        for i in range(int(Config.LOOKBACK_HOURS * 4) + 1, 0, -1):
            past = now_utc - timedelta(seconds=i * interval_sec)
            ts = int(past.timestamp()) - (int(past.timestamp()) % interval_sec)
            timestamps.append(ts)
        
        # Timestamp actual (redondeado)
        current_ts = int(now_utc.timestamp()) - (int(now_utc.timestamp()) % interval_sec)
        timestamps.append(current_ts)
        
        # Timestamps futuros (1.5 horas = 6 intervalos)
        for i in range(1, int(Config.LOOKAHEAD_HOURS * 4) + 1):
            future = now_utc + timedelta(seconds=i * interval_sec)
            ts = int(future.timestamp()) - (int(future.timestamp()) % interval_sec)
            timestamps.append(ts)
        
        return sorted(set(timestamps))
    
    def get_btc_15m_markets(self, force=False):
        """
        Obtiene mercados BTC 15m usando cache inteligente
        - force=True: Ignora cache y busca nuevos
        - force=False: Usa cache si no ha expirado
        """
        now = time.time()
        
        # Usa cache si no ha expirado
        if not force and now - self.cache_time < Config.CACHE_REFRESH_SEC * 2:
            print(f"♻️ Usando cache ({len(self.cache)} mercados)")
            return self.cache
        
        print("🔍 Generando slugs dinámicos...")
        timestamps = self.generate_timestamps()
        btc_markets = []
        
        # Busca cada mercado por su slug
        for ts in timestamps:
            slug = f"{Config.SERIES_PATTERN}{ts}"
            m = self.get_market_by_slug(slug)
            if m:
                btc_markets.append(m)
                print(f"   ✅ Encontrado: {slug}")
        
        self.cache = btc_markets
        self.cache_time = now
        print(f"🔄 Total encontrados: {len(btc_markets)}")
        return btc_markets
    
    def get_market_by_slug(self, slug):
        """Busca mercado individual por slug en Gamma API"""
        try:
            r = requests.get(f"{Config.GAMMA_API}/markets/slug/{slug}", timeout=5)
            if r.status_code == 200:
                return r.json()
            return None
        except Exception as e:
            # Silencioso para no saturar logs
            return None
    
    def get_next_active_market(self):
        """
        Encuentra el siguiente mercado activo
        Criterios:
        - Debe estar activo o empezar en <20 min (1200 seg)
        - No debe cerrar en <30 segundos
        - Ordena por tiempo de cierre (próximo a cerrar primero)
        """
        markets = self.get_btc_15m_markets(force=True)
        if not markets:
            return []
        
        now = datetime.now(timezone.utc)
        candidates = []
        
        for m in markets:
            try:
                end_str = m.get('endDate')
                if not end_str: 
                    continue
                
                end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
                to_end = (end_dt - now).total_seconds()
                
                # Ignora mercados que cierran en <30 seg
                if to_end <= 30: 
                    continue
                
                start_str = m.get('startDate') or m.get('eventStartTime') or end_str
                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                to_start = (start_dt - now).total_seconds()
                
                # Acepta mercados que empiezan en <20 min Y cierran en >30 seg
                if to_start < 1200 and to_end > 30:
                    candidates.append({
                        'market': m, 
                        'to_end': to_end, 
                        'to_start': to_start
                    })
            except Exception as e:
                print(f"⚠️ Error procesando mercado: {e}")
                continue
        
        # Ordena por tiempo de cierre (menor primero)
        candidates.sort(key=lambda x: x['to_end'])
        
        # Guarda próximos 5 mercados
        self.upcoming = candidates[1:6] if len(candidates) > 1 else []
        
        return candidates
    
    def should_switch_market(self):
        """
        Verifica si debe cambiar de mercado
        Cambia si:
        - No hay mercado seleccionado
        - El mercado actual cierra en <2 minutos (120 seg)
        """
        if not self.selected_market: 
            return True
        try:
            end_str = self.selected_market.get('endDate')
            end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
            secs_left = (end_dt - datetime.now(timezone.utc)).total_seconds()
            return secs_left < 120
        except:
            return True
    
    def auto_switch_to_next_market(self):
        """Cambia automáticamente al siguiente mercado activo"""
        print("\n🔄 Buscando siguiente BTC 15m...")
        cands = self.get_next_active_market()
        
        if not cands:
            print("❌ No hay mercados disponibles ahora")
            self.selected_market = None
            self.selected_token_ids = None
            return False
        
        info = cands[0]
        m = info['market']
        self.selected_market = m
        
        # Parsea token IDs (YES/NO)
        try:
            self.selected_token_ids = json.loads(m.get('clobTokenIds', '[]'))
        except:
            self.selected_token_ids = None
        
        self.show_detailed_preview(m)
        return True
    
    def show_detailed_preview(self, market):
        """Muestra información detallada del mercado"""
        if not market: 
            return
        
        print("\n" + "="*90)
        print(f"🖼️ MERCADO ACTUAL: {market.get('question', 'N/A')}")
        print(f"Slug: {market.get('slug', 'N/A')}")
        
        # Timer y fecha de cierre
        end_str = market.get('endDate', '')
        timer, urgent = self.calculate_timer(end_str)
        bog_tz = pytz.timezone('America/Bogota')
        
        try:
            end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
            end_bog = end_dt.astimezone(bog_tz).strftime("%I:%M %p %Z - %d %b")
            urgency_marker = '⚠️ Muy pronto' if urgent else '✅ Activo'
            print(f"Cierre: {timer} ({urgency_marker}) → {end_bog}")
        except:
            print("Cierre: ??")
        
        # Precios UP/DOWN
        up, down = self.parse_outcome_prices(market)
        print(f"Up:    {up*100:5.1f}¢   ({up:.4f})")
        print(f"Down:  {down*100:5.1f}¢   ({down:.4f})")
        
        # Volumen y liquidez
        vol = market.get('volumeNum', market.get('volume24hr', 0))
        liq = market.get('liquidityNum', 'N/A')
        print(f"Volumen: ${vol:,.0f}" if isinstance(vol, (int, float)) else f"Volumen: {vol}")
        print(f"Liquidez: ${liq:,.2f}" if isinstance(liq, (int, float)) else f"Liquidez: {liq}")
        
        # Midpoint y spread (solo si hay tokens)
        if self.selected_token_ids and len(self.selected_token_ids) > 0:
            yes_token = self.selected_token_ids[0]
            try:
                mid = self.read_client.get_midpoint(yes_token)
                spread = self.read_client.get_spread(yes_token)
                print(f"Midpoint: {mid.get('mid', 'N/A')}")
                print(f"Spread:   {spread.get('spread', 'N/A')}")
            except Exception as e:
                print(f"Midpoint/Spread: No disponible")
        
        # Token IDs
        print(f"Tokens:")
        if self.selected_token_ids:
            print(f"   YES/Up:  {self.selected_token_ids[0]}")
            if len(self.selected_token_ids) > 1:
                print(f"   NO/Down: {self.selected_token_ids[1]}")
        else:
            print("   No disponibles")
        
        print("="*90 + "\n")
    
    def parse_outcome_prices(self, market):
        """Extrae precios UP/DOWN del mercado"""
        s = market.get('outcomePrices', '["0.5","0.5"]')
        try:
            prices = json.loads(s)
            up = float(prices[0])
            down = float(prices[1]) if len(prices) > 1 else 0.5
            return up, down
        except:
            return 0.5, 0.5
    
    def calculate_timer(self, end_str):
        """
        Calcula tiempo restante hasta cierre
        Retorna: (timer_str, is_urgent)
        """
        try:
            end = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
            rem = end - datetime.now(timezone.utc)
            
            if rem.total_seconds() <= 0:
                return "Cerrado", True
            
            mins = int(rem.total_seconds() // 60)
            return f"{mins}m", rem.total_seconds() < 300  # Urgente si <5 min
        except:
            return "??:??", False
    
    def check_mt4_signals(self):
        """
        Lee el CSV de MT4 y procesa nuevas señales.
        - Asume símbolo BTC-related.
        - "call" -> BUY YES (Up)
        - "put" -> BUY NO (Down)
        - Usa monto configurado en self.trade_amount
        - Solo procesa señales nuevas (timestamp > último procesado)
        """
        global Config  # Para actualizar LAST_TIMESTAMP
        
        if not os.path.exists(Config.CSV_PATH):
            print("❌ CSV de MT4 no encontrado en:", Config.CSV_PATH)
            return
        
        try:
            with open(Config.CSV_PATH, 'r') as file:
                reader = csv.reader(file)
                next(reader)  # Salta encabezado: tempo,ativo,acao,expiracao,estrategia
                
                for row in reader:
                    if not row: continue
                    timestamp, symbol, action, expiration, strategy = row
                    ts = int(timestamp)
                    
                    if ts > Config.LAST_TIMESTAMP and symbol.lower().startswith('btc'):  # Asume símbolo BTC
                        print(f"\n🚨 Nueva señal de MT4 detectada: {symbol} - {action.upper()} - Exp: {expiration} min - Estrategia: {strategy}")
                        
                        if not self.selected_token_ids or len(self.selected_token_ids) < 2:
                            print("❌ No hay mercado seleccionado con tokens válidos. No se puede ejecutar trade.")
                            continue
                        
                        bal = self.get_balance() or 0
                        if bal < self.trade_amount:
                            print(f"❌ Balance insuficiente para ${self.trade_amount} trade.")
                            continue
                        
                        # Mapeo: call -> BUY YES (Up), put -> BUY NO (Down)
                        if action.lower() == "call":
                            token_id = self.selected_token_ids[0]  # YES/Up
                            side = "BUY"
                        elif action.lower() == "put":
                            token_id = self.selected_token_ids[1]  # NO/Down
                            side = "BUY"
                        else:
                            print("❌ Acción inválida en señal:", action)
                            continue
                        
                        # Ejecuta orden con monto configurado
                        self.place_market_order(token_id, self.trade_amount, side)
                        
                        # Actualiza último timestamp procesado
                        Config.LAST_TIMESTAMP = ts
                        
        except Exception as e:
            print(f"❌ Error leyendo CSV de MT4: {e}")
    
    def monitor_mode(self):
        """
        Modo monitor continuo
        - Actualiza cada 30 segundos
        - Auto-switch cuando mercado cierra en <2 min
        - Verifica señales de MT4 en cada iteración
        - Ctrl+C para salir
        """
        print("\n" + "="*90)
        print("🔍 MODO MONITOR ACTIVADO")
        print(f"⏱️ Actualiza cada {Config.MONITOR_INTERVAL_SEC}s")
        print("🔄 Auto-switch cuando cierre <2 min")
        print("📡 Monitoreando señales de MT4 en: {Config.CSV_PATH}")
        print(f"💰 Monto por trade: ${self.trade_amount}")
        print("⌨️ Ctrl+C para salir")
        print("="*90)
        
        while True:
            try:
                # Verifica señales de MT4
                self.check_mt4_signals()
                
                # Verifica si debe cambiar de mercado
                if self.should_switch_market():
                    print("\n⚠️ Mercado cerrando → cambiando automáticamente...")
                    self.auto_switch_to_next_market()
                else:
                    # Actualiza vista del mercado actual
                    print("\n" + "-"*90)
                    now_str = datetime.now(pytz.timezone('America/Bogota')).strftime('%H:%M:%S -05')
                    print(f"[{now_str}] Actualizando mercado actual...")
                    self.show_detailed_preview(self.selected_market)
                
                time.sleep(Config.MONITOR_INTERVAL_SEC)
                
            except KeyboardInterrupt:
                print("\n\n⏹️ Modo monitor detenido por usuario.")
                break
            except Exception as e:
                print(f"❌ Error en monitor: {e}")
                time.sleep(10)
    
    def get_orderbook(self, token_id, depth=5):
        """Muestra order book (libro de órdenes) del token"""
        try:
            book = self.read_client.get_order_book(token_id)
            asks = sorted(book.asks, key=lambda x: float(x.price))
            bids = sorted(book.bids, key=lambda x: float(x.price), reverse=True)
            
            print("\n" + "="*70)
            print(f"📖 ORDER BOOK para token {token_id[:12]}...")
            print("\nASKS (venta):")
            for a in asks[:depth]:
                print(f"  {float(a.price):.4f} | Size: {a.size}")
            
            print("\nBIDS (compra):")
            for b in bids[:depth]:
                print(f"  {float(b.price):.4f} | Size: {b.size}")
            print("="*70)
        except Exception as e:
            print(f"❌ Error obteniendo orderbook: {e}")
    
    def place_market_order(self, token_id, amount, side):
        """
        Coloca orden de mercado
        - token_id: ID del token YES o NO
        - amount: Monto en USDC
        - side: "BUY" o "SELL"
        """
        if not self.auth_client:
            print("❌ No autenticado para trading")
            return
        
        try:
            s_const = BUY if side.upper() == "BUY" else SELL
            
            mo = MarketOrderArgs(
                token_id=token_id,
                amount=float(amount),
                side=s_const,
                order_type=OrderType.FOK  # Fill-Or-Kill
            )
            
            signed = self.auth_client.create_market_order(mo)
            resp = self.auth_client.post_order(signed, OrderType.FOK)
            
            print("✅ Orden ejecutada:")
            print(f"   Token: {token_id[:16]}...")
            print(f"   Lado: {side}")
            print(f"   Monto: ${amount}")
            print(f"   Respuesta: {resp}")
        except Exception as e:
            print(f"❌ Error ejecutando orden: {e}")

# ==================== MENÚ ====================
def main_menu():
    print("\n" + "="*90)
    print("🎯 POLYMARKET BTC 15m BOT - MONITOR + INFO EXTENDIDA")
    print("="*90)
    
    trader = PolymarketTrader()
    
    # Auto-switch inicial si está habilitado
    if Config.AUTO_SWITCH_ENABLED:
        print("\n🔄 Auto-switch habilitado")
        trader.auto_switch_to_next_market()
    
    while True:
        # Verifica si debe cambiar de mercado
        if Config.AUTO_SWITCH_ENABLED and trader.should_switch_market():
            print("\n⚠️ Mercado cerrando → switch automático...")
            trader.auto_switch_to_next_market()
        
        # Muestra menú
        print("\n" + "="*90)
        print("📋 MENÚ PRINCIPAL")
        
        if trader.selected_market:
            q = trader.selected_market.get('question', '?')[:50]
            t, _ = trader.calculate_timer(trader.selected_market.get('endDate', ''))
            print(f"   Mercado actual: {q}... ({t})")
        
        print("="*90)
        print("[1] Ver balance")
        print("[2] Buscar mercado por slug")
        print("[3] Ver detalles del mercado actual")
        print("[4] Ver order book")
        print("[5] Colocar orden")
        print("[6] Ver próximos mercados")
        print("[7] Forzar cambio de mercado")
        print("[8] MODO MONITOR (auto-refresh + MT4 signals)")
        print("[0] Salir")
        print("="*90)
        
        opt = input("\n➤ Opción: ").strip()
        
        if opt == "1":
            trader.show_balance()
        
        elif opt == "2":
            slug = input("Ingresa slug del mercado: ").strip()
            if slug:
                m = trader.get_market_by_slug(slug)
                if m:
                    trader.show_detailed_preview(m)
                    if input("¿Seleccionar este mercado? (s/n): ").lower() == 's':
                        trader.selected_market = m
                        try:
                            trader.selected_token_ids = json.loads(m.get('clobTokenIds', '[]'))
                            print("✅ Mercado seleccionado")
                        except:
                            print("⚠️ No se pudieron cargar tokens")
                else:
                    print("❌ Mercado no encontrado")
        
        elif opt == "3":
            if trader.selected_market:
                trader.show_detailed_preview(trader.selected_market)
            else:
                print("❌ No hay mercado seleccionado")
        
        elif opt == "4":
            if not trader.selected_token_ids or len(trader.selected_token_ids) == 0:
                print("❌ Selecciona un mercado con tokens válidos primero")
                continue
            
            print("\n¿Qué token quieres ver?")
            print("[1] YES/Up")
            print("[2] NO/Down")
            choice = input("Opción: ").strip()
            
            if choice == "1":
                trader.get_orderbook(trader.selected_token_ids[0])
            elif choice == "2" and len(trader.selected_token_ids) > 1:
                trader.get_orderbook(trader.selected_token_ids[1])
            else:
                print("❌ Opción inválida")
        
        elif opt == "5":
            if not trader.selected_token_ids or len(trader.selected_token_ids) < 2:
                print("❌ Selecciona un mercado con tokens válidos primero")
                continue
            
            bal = trader.get_balance() or 0
            print(f"\n💰 Balance disponible: ${bal:.2f} USDC")
            
            if bal <= 0:
                print("⚠️ No tienes balance suficiente para operar")
                continue
            
            print("\n¿En qué outcome quieres operar?")
            print("[1] Up (YES)")
            print("[2] Down (NO)")
            outcome = input("Opción: ").strip()
            
            if outcome not in ["1", "2"]:
                print("❌ Opción inválida")
                continue
            
            token = trader.selected_token_ids[0 if outcome == "1" else 1]
            
            print("\n¿Qué operación quieres hacer?")
            print("[1] BUY (comprar)")
            print("[2] SELL (vender)")
            side_choice = input("Opción: ").strip()
            
            if side_choice == "1":
                side = "BUY"
            elif side_choice == "2":
                side = "SELL"
            else:
                print("❌ Opción inválida")
                continue
            
            amt_str = input(f"\nMonto en USDC (máx ${bal:.2f}): $").strip()
            
            try:
                amt = float(amt_str)
                if amt <= 0:
                    print("❌ El monto debe ser mayor a 0")
                    continue
                if amt > bal:
                    print(f"❌ Monto excede tu balance (${bal:.2f})")
                    continue
                
                # Confirmación
                outcome_name = "Up" if outcome == "1" else "Down"
                print(f"\n⚠️ CONFIRMACIÓN:")
                print(f"   Operación: {side}")
                print(f"   Outcome: {outcome_name}")
                print(f"   Monto: ${amt:.2f} USDC")
                confirm = input("\n¿Confirmar orden? (s/n): ").strip().lower()
                
                if confirm == 's':
                    trader.place_market_order(token, amt, side)
                else:
                    print("❌ Orden cancelada")
                    
            except ValueError:
                print("❌ Monto inválido")
        
        elif opt == "6":
            if trader.upcoming:
                print("\n" + "="*90)
                print("📅 PRÓXIMOS MERCADOS BTC 15m")
                print("="*90)
                
                for idx, c in enumerate(trader.upcoming, 1):
                    m = c['market']
                    mins = int(c['to_end'] // 60)
                    slug = m.get('slug', 'N/A')
                    question = m.get('question', 'N/A')[:60]
                    
                    print(f"\n[{idx}] {question}...")
                    print(f"    Cierra en: ~{mins} minutos")
                    print(f"    Slug: {slug}")
                
                print("="*90)
            else:
                print("❌ No hay próximos mercados en caché")
                print("💡 Ejecuta opción [7] para actualizar")
        
        elif opt == "7":
            trader.auto_switch_to_next_market()
        
        elif opt == "8":
            bal = trader.get_balance() or 0
            print(f"\n💰 Balance disponible: ${bal:.2f} USDC")
            amt_str = input("\nIngresa el monto en USDC para cada operación automática en modo monitor: $").strip()
            try:
                amt = float(amt_str)
                if amt <= 0:
                    print("❌ El monto debe ser mayor a 0. Usando predeterminado $1.")
                    trader.trade_amount = 1.0
                elif amt > bal:
                    print(f"❌ Monto excede tu balance (${bal:.2f}). Usando predeterminado $1.")
                    trader.trade_amount = 1.0
                else:
                    trader.trade_amount = amt
                    print(f"✅ Monto configurado: ${amt:.2f}")
            except ValueError:
                print("❌ Monto inválido. Usando predeterminado $1.")
                trader.trade_amount = 1.0
            trader.monitor_mode()
        
        elif opt == "0":
            print("\n" + "="*90)
            print("👋 ¡Hasta la próxima!")
            print("="*90)
            break
        
        else:
            print("❌ Opción no válida")


if __name__ == "__main__":

    main_menu()
