使用WebSocket（AsyncWeb3.WebSocketProvider）来做pool合约的实时日志数据的收集
考虑的问题：websocket断联重连，然后通过get_logs（HTTP）补充遗漏数据。
考虑是否可以同时订阅多个池子
```
{
  "address": [pool1, pool2, pool3],
  "topics": [swap_topic]
}
```