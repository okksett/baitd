# Firma de los releases

Desde la 0.6.1, el actualizador de Baitd **solo instala un release firmado**. Antes de instalar comprueba dos cosas:

1. que `SHA256SUMS` tenga una firma Ed25519 válida (`SHA256SUMS.sig`), hecha con la clave de quien publica;
2. que `Baitd-Setup.exe` coincida con la huella de ese `SHA256SUMS`.

Así, quien tenga acceso a la cuenta de GitHub ya no puede hacer que las copias instaladas ejecuten un instalador suyo.

## Dónde está la clave

- **Clave privada:** `%USERPROFILE%\.baitd-firma\baitd-release-key.pem`. Está fuera del repositorio y fuera de OneDrive, y nunca va a GitHub.
- **Clave pública:** `PUBLIC_KEY` en `src/baitd/updater.py`.

**Haz una copia de seguridad de la clave privada**, por ejemplo en un USB o en un gestor de contraseñas. Si se pierde, no se pueden firmar más versiones, y las copias instaladas rechazarán la siguiente actualización. En ese caso, cada usuario tendría que instalar la nueva a mano.

## En cada release

1. Espera a que el CI pase en verde y baja el artefacto `Baitd-checksums`. Trae el `SHA256SUMS` de los 5 archivos.
2. Firma ese archivo:

   ```bash
   python tools/sign_release.py sign ruta/a/SHA256SUMS
   ```

   Esto crea `SHA256SUMS.sig` al lado.
3. Sube al release los 5 archivos, `SHA256SUMS` y `SHA256SUMS.sig`.

## Cambiar de clave

Si la clave se filtra:

1. Crea otra con `python tools/sign_release.py keygen`. No sobrescribe la actual: muévela antes a otro sitio.
2. Pon la nueva clave pública en `PUBLIC_KEY`.
3. Publica esa versión firmada **con la clave vieja**, para que las copias instaladas la acepten.

Desde esa versión en adelante, se firma con la nueva.
