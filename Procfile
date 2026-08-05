
redis_cache: redis-server config/redis_cache.conf
redis_queue: redis-server config/redis_queue.conf


web: /home/vishal/.local/bin/bench serve  --port 8001


socketio: /usr/bin/node apps/frappe/socketio.js


watch: /home/vishal/.local/bin/bench watch


schedule: while true; do /home/vishal/.local/bin/bench schedule; sleep 1; done


worker: while true; do /home/vishal/.local/bin/bench worker; sleep 1; done 1>> logs/worker.log 2>> logs/worker.error.log
