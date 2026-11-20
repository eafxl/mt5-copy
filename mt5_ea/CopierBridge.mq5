//+------------------------------------------------------------------+
//|                                                CopierBridge.mq5   |
//|   Master-side push bridge for the MT5 copy-trading service.      |
//|                                                                  |
//|   Fires on OnTradeTransaction (the earliest possible signal —    |
//|   a deal is added the instant a position opens) and pushes the   |
//|   event over a localhost TCP socket to the Python service, so    |
//|   copies happen in ~ms instead of a poll cycle. MQL5 sockets are |
//|   client-only, so Python is the server and this EA connects out. |
//|                                                                  |
//|   Protocol: one newline-terminated JSON object per event:        |
//|   {"event":"open","ticket":..,"symbol":"..","type":0/1,          |
//|    "volume":..,"price":..,"sl":..,"tp":..,"time_msc":..,          |
//|    "magic":..,"comment":".."}                                     |
//|   events: open | modify | close | heartbeat                      |
//+------------------------------------------------------------------+
#property strict
#property version   "1.00"

input string InpHost = "127.0.0.1";   // Python listener host
input int    InpPort = 47800;          // Python listener port
input int    InpReconnectMs = 200;     // reconnect check interval (ms)
input int    InpHeartbeatSec = 5;      // heartbeat interval
input int    InpConnectTimeoutMs = 300; // per-attempt connect timeout (ms)

int    g_socket = INVALID_HANDLE;
bool   g_connected = false;
long   g_last_heartbeat_ms = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   // Millisecond timer -> reconnect within ~InpReconnectMs of a drop.
   // Trade events still push INSTANTLY via OnTradeTransaction (not the timer),
   // so copy latency is unaffected by this interval.
   EventSetMillisecondTimer(MathMax(20, InpReconnectMs));
   Connect();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Disconnect();
}

//+------------------------------------------------------------------+
//| Connection management                                            |
//+------------------------------------------------------------------+
void Connect()
{
   if(g_connected)
      return;
   g_socket = SocketCreate();
   if(g_socket == INVALID_HANDLE)
   {
      PrintFormat("CopierBridge: SocketCreate failed err=%d "
                  "(enable 127.0.0.1 in Tools>Options>Expert Advisors 'Allow ...')", GetLastError());
      return;
   }
   if(!SocketConnect(g_socket, InpHost, InpPort, InpConnectTimeoutMs))
   {
      PrintFormat("CopierBridge: connect %s:%d failed err=%d", InpHost, InpPort, GetLastError());
      SocketClose(g_socket);
      g_socket = INVALID_HANDLE;
      return;
   }
   g_connected = true;
   PrintFormat("CopierBridge: connected to %s:%d", InpHost, InpPort);
}

void Disconnect()
{
   if(g_socket != INVALID_HANDLE)
      SocketClose(g_socket);
   g_socket = INVALID_HANDLE;
   g_connected = false;
}

bool SendLine(const string payload)
{
   if(!g_connected)
      Connect();
   if(!g_connected)
      return(false);
   string line = payload + "\n";
   uchar buf[];  // SocketSend requires uchar[]
   int len = StringToCharArray(line, buf, 0, WHOLE_ARRAY, CP_UTF8) - 1; // drop null terminator
   if(len <= 0)
      return(false);
   int sent = SocketSend(g_socket, buf, len);
   if(sent != len)
   {
      PrintFormat("CopierBridge: send failed sent=%d/%d err=%d — dropping link", sent, len, GetLastError());
      Disconnect();     // force reconnect on next event/timer
      return(false);
   }
   return(true);
}

//+------------------------------------------------------------------+
//| Timer: heartbeat + auto-reconnect (self-heal on drops)           |
//+------------------------------------------------------------------+
void OnTimer()
{
   // Fast reconnect: fires every InpReconnectMs, so a dropped link is
   // re-established within ~InpReconnectMs.
   if(!g_connected)
   {
      Connect();
      return;
   }
   // Heartbeat only every InpHeartbeatSec (not every ms tick).
   long now_ms = (long)GetTickCount();
   if(now_ms - g_last_heartbeat_ms >= (long)InpHeartbeatSec * 1000)
   {
      g_last_heartbeat_ms = now_ms;
      // Epoch SECONDS as a number, *1000 -> ms. (string)TimeCurrent() would
      // render a datetime "2026.08.05 18:56:32" and break JSON.
      SendLine("{\"event\":\"heartbeat\",\"ts_msc\":" + (string)((long)TimeCurrent() * 1000) + "}");
   }
}

//+------------------------------------------------------------------+
//| The earliest trade signal — a deal was added / position changed  |
//+------------------------------------------------------------------+
void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD)
   {
      // A deal hit the books: open (ENTRY_IN) or close (ENTRY_OUT).
      if(!HistoryDealSelect(trans.deal))
         return;
      long entry = HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
      long pos_id = HistoryDealGetInteger(trans.deal, DEAL_POSITION_ID);
      if(entry == DEAL_ENTRY_IN)
         EmitPosition("open", pos_id);
      else if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
      {
         // If the position still exists after the deal, it was a PARTIAL
         // close (scale-out / partial take-profit) — push the new volume.
         // Otherwise it's a full close.
         if(PositionSelectByTicket(pos_id))
            EmitPosition("reduce", pos_id);
         else
            EmitClose(pos_id);
      }
   }
   else if(trans.type == TRADE_TRANSACTION_POSITION)
   {
      // SL/TP (or volume) modified on an existing position.
      EmitPosition("modify", trans.position);
   }
}

//+------------------------------------------------------------------+
//| Serialize + push a position snapshot                             |
//+------------------------------------------------------------------+
void EmitPosition(const string event, const long ticket)
{
   if(ticket <= 0 || !PositionSelectByTicket(ticket))
      return;
   int    type   = (int)PositionGetInteger(POSITION_TYPE);   // 0=BUY,1=SELL
   double volume = PositionGetDouble(POSITION_VOLUME);
   double price  = PositionGetDouble(POSITION_PRICE_OPEN);
   double sl     = PositionGetDouble(POSITION_SL);
   double tp     = PositionGetDouble(POSITION_TP);
   long   tmsc   = PositionGetInteger(POSITION_TIME_MSC);
   long   magic  = PositionGetInteger(POSITION_MAGIC);
   string sym    = PositionGetString(POSITION_SYMBOL);
   string cmt    = PositionGetString(POSITION_COMMENT);
   int    digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

   string json = StringFormat(
      "{\"event\":\"%s\",\"ticket\":%I64d,\"symbol\":\"%s\",\"type\":%d,"
      "\"volume\":%s,\"price\":%s,\"sl\":%s,\"tp\":%s,"
      "\"time_msc\":%I64d,\"magic\":%I64d,\"comment\":\"%s\"}",
      event, ticket, sym, type,
      DoubleToString(volume, 2), DoubleToString(price, digits),
      DoubleToString(sl, digits), DoubleToString(tp, digits),
      tmsc, magic, cmt);
   SendLine(json);
}

void EmitClose(const long ticket)
{
   string json = StringFormat("{\"event\":\"close\",\"ticket\":%I64d}", ticket);
   SendLine(json);
}
//+------------------------------------------------------------------+