module.exports = {
  apps: [
    {
      name: "bt-copytrader",
      script: "copytrader_all.py",
      args: "run --summary-now",
      interpreter: "python3",
      watch: false,
      autorestart: true,
      max_restarts: 10,
      restart_delay: 3000
    }
  ]
}
