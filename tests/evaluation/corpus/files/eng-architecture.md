# Platform architecture reference

This reference lists every service of the platform with its owner, port and availability target. It is long on purpose: look up the service you need by name.

## Service atlas

The atlas service is owned by the Core team and is written in Go. It listens on port 7011 and keeps its state in PostgreSQL.

The availability target of the atlas service is 99.5 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Core team is paged. Deployments of the atlas service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the atlas service is reviewed every quarter; the current request budget is 360 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service beacon

The beacon service is owned by the Payments team and is written in Java. It listens on port 7048 and keeps its state in an in-memory cache.

The availability target of the beacon service is 99.9 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Payments team is paged. Deployments of the beacon service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the beacon service is reviewed every quarter; the current request budget is 480 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service cinder

The cinder service is owned by the Search team and is written in Python. It listens on port 7085 and keeps its state in SQLite.

The availability target of the cinder service is 99.95 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Search team is paged. Deployments of the cinder service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the cinder service is reviewed every quarter; the current request budget is 600 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service drift

The drift service is owned by the Identity team and is written in TypeScript. It listens on port 7122 and keeps its state in a Kafka topic.

The availability target of the drift service is 99.99 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Identity team is paged. Deployments of the drift service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the drift service is reviewed every quarter; the current request budget is 720 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service ember

The ember service is owned by the Storage team and is written in Rust. It listens on port 7159 and keeps its state in object storage.

The availability target of the ember service is 99.5 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Storage team is paged. Deployments of the ember service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the ember service is reviewed every quarter; the current request budget is 840 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service fathom

The fathom service is owned by the Messaging team and is written in Go. It listens on port 7196 and keeps its state in Redis.

The availability target of the fathom service is 99.9 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Messaging team is paged. Deployments of the fathom service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the fathom service is reviewed every quarter; the current request budget is 960 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service garnet

The garnet service is owned by the Core team and is written in Java. It listens on port 7233 and keeps its state in PostgreSQL.

The availability target of the garnet service is 99.95 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Core team is paged. Deployments of the garnet service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the garnet service is reviewed every quarter; the current request budget is 1080 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service harbor

The harbor service is owned by the Payments team and is written in Python. It listens on port 7270 and keeps its state in an in-memory cache.

The availability target of the harbor service is 99.99 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Payments team is paged. Deployments of the harbor service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the harbor service is reviewed every quarter; the current request budget is 1200 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service ibis

The ibis service is owned by the Search team and is written in TypeScript. It listens on port 7307 and keeps its state in SQLite.

The availability target of the ibis service is 99.5 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Search team is paged. Deployments of the ibis service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the ibis service is reviewed every quarter; the current request budget is 1320 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service juniper

The juniper service is owned by the Identity team and is written in Rust. It listens on port 7344 and keeps its state in a Kafka topic.

The availability target of the juniper service is 99.9 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Identity team is paged. Deployments of the juniper service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the juniper service is reviewed every quarter; the current request budget is 1440 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service kelp

The kelp service is owned by the Storage team and is written in Go. It listens on port 7381 and keeps its state in object storage.

The availability target of the kelp service is 99.95 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Storage team is paged. Deployments of the kelp service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the kelp service is reviewed every quarter; the current request budget is 1560 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service ledger

The ledger service is owned by the Messaging team and is written in Java. It listens on port 7418 and keeps its state in Redis.

The availability target of the ledger service is 99.99 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Messaging team is paged. Deployments of the ledger service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the ledger service is reviewed every quarter; the current request budget is 1680 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service meridian

The meridian service is owned by the Core team and is written in Python. It listens on port 7455 and keeps its state in PostgreSQL.

The availability target of the meridian service is 99.5 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Core team is paged. Deployments of the meridian service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the meridian service is reviewed every quarter; the current request budget is 1800 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service nimbus

The nimbus service is owned by the Payments team and is written in TypeScript. It listens on port 7492 and keeps its state in an in-memory cache.

The availability target of the nimbus service is 99.9 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Payments team is paged. Deployments of the nimbus service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the nimbus service is reviewed every quarter; the current request budget is 1920 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service onyx

The onyx service is owned by the Search team and is written in Rust. It listens on port 7529 and keeps its state in SQLite.

The availability target of the onyx service is 99.95 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Search team is paged. Deployments of the onyx service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the onyx service is reviewed every quarter; the current request budget is 2040 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service prism

The prism service is owned by the Identity team and is written in Go. It listens on port 7566 and keeps its state in a Kafka topic.

The availability target of the prism service is 99.99 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Identity team is paged. Deployments of the prism service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the prism service is reviewed every quarter; the current request budget is 2160 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service quartz

The quartz service is owned by the Storage team and is written in Java. It listens on port 7603 and keeps its state in object storage.

The availability target of the quartz service is 99.5 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Storage team is paged. Deployments of the quartz service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the quartz service is reviewed every quarter; the current request budget is 2280 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service relay

The relay service is owned by the Messaging team and is written in Python. It listens on port 7640 and keeps its state in Redis.

The availability target of the relay service is 99.9 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Messaging team is paged. Deployments of the relay service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the relay service is reviewed every quarter; the current request budget is 2400 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service sable

The sable service is owned by the Core team and is written in TypeScript. It listens on port 7677 and keeps its state in PostgreSQL.

The availability target of the sable service is 99.95 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Core team is paged. Deployments of the sable service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the sable service is reviewed every quarter; the current request budget is 2520 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service tundra

The tundra service is owned by the Payments team and is written in Rust. It listens on port 7714 and keeps its state in an in-memory cache.

The availability target of the tundra service is 99.99 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Payments team is paged. Deployments of the tundra service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the tundra service is reviewed every quarter; the current request budget is 2640 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service umbra

The umbra service is owned by the Search team and is written in Go. It listens on port 7751 and keeps its state in SQLite.

The availability target of the umbra service is 99.5 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Search team is paged. Deployments of the umbra service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the umbra service is reviewed every quarter; the current request budget is 2760 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service vector

The vector service is owned by the Identity team and is written in Java. It listens on port 7788 and keeps its state in a Kafka topic.

The availability target of the vector service is 99.9 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Identity team is paged. Deployments of the vector service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the vector service is reviewed every quarter; the current request budget is 2880 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service willow

The willow service is owned by the Storage team and is written in Python. It listens on port 7825 and keeps its state in object storage.

The availability target of the willow service is 99.95 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Storage team is paged. Deployments of the willow service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the willow service is reviewed every quarter; the current request budget is 3000 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service xenon

The xenon service is owned by the Messaging team and is written in TypeScript. It listens on port 7862 and keeps its state in Redis.

The availability target of the xenon service is 99.99 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Messaging team is paged. Deployments of the xenon service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the xenon service is reviewed every quarter; the current request budget is 3120 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service yarrow

The yarrow service is owned by the Core team and is written in Rust. It listens on port 7899 and keeps its state in PostgreSQL.

The availability target of the yarrow service is 99.5 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Core team is paged. Deployments of the yarrow service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the yarrow service is reviewed every quarter; the current request budget is 3240 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service zephyr

The zephyr service is owned by the Payments team and is written in Go. It listens on port 7036 and keeps its state in an in-memory cache.

The availability target of the zephyr service is 99.9 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Payments team is paged. Deployments of the zephyr service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the zephyr service is reviewed every quarter; the current request budget is 3360 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service anchor

The anchor service is owned by the Search team and is written in Java. It listens on port 7073 and keeps its state in SQLite.

The availability target of the anchor service is 99.95 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Search team is paged. Deployments of the anchor service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the anchor service is reviewed every quarter; the current request budget is 3480 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service bramble

The bramble service is owned by the Identity team and is written in Python. It listens on port 7110 and keeps its state in a Kafka topic.

The availability target of the bramble service is 99.99 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Identity team is paged. Deployments of the bramble service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the bramble service is reviewed every quarter; the current request budget is 3600 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service cobalt

The cobalt service is owned by the Storage team and is written in TypeScript. It listens on port 7147 and keeps its state in object storage.

The availability target of the cobalt service is 99.5 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Storage team is paged. Deployments of the cobalt service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the cobalt service is reviewed every quarter; the current request budget is 3720 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.

## Service dune

The dune service is owned by the Messaging team and is written in Rust. It listens on port 7184 and keeps its state in Redis.

The availability target of the dune service is 99.9 percent per month. When the service fails its health check three times in a row, the on-call engineer of the Messaging team is paged. Deployments of the dune service follow the standard pipeline and are announced in the team channel one day ahead.

Capacity planning for the dune service is reviewed every quarter; the current request budget is 3840 requests per second, and any sustained use above 70 percent of the budget triggers a scaling review.
