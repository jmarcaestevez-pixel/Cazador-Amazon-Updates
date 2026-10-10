# Cazador Amazon — actualizaciones

**Canal operativo para actualizar el código de la aplicación:** `manifest_source.json` contiene la versión, URL HTTPS fija y SHA-256 de `cazador_integrado.py`. El lanzador Windows compilado en Go consulta este manifiesto en cada arranque y conserva la base de datos en `%LOCALAPPDATA%\CazadorAmazon\precios.sqlite`.

El lanzador Windows está compilado, pero aún no ha sido probado físicamente en Windows. Requiere Python 3, ya utilizado por Cazador 1.3. El `.exe` permanece fijo; se actualiza automáticamente el código Python de la aplicación, **no el propio lanzador**.

El canal confía en los permisos del repositorio GitHub y la comprobación SHA-256; no dispone todavía de una firma independiente. No publicar credenciales ni bases de datos. El código del buscador sigue siendo experimental: detectar ASIN no garantiza nombres, precios ni descuentos del 70 %.
