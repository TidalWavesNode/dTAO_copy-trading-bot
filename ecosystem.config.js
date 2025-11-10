---

### `ecosystem.config.js`
```js
module.exports = {
  apps: [
    {
      name: "bt-taoplicate",
      script: "taoplicate.py",
      args: "run --summary-now",
      interpreter: "python3",
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000
    }
  ]
}
