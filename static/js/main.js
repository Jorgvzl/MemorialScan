document.addEventListener('DOMContentLoaded', function() {
    // Busca el botón para compartir en la página
    const shareButton = document.getElementById('share-button');
    // Busca la barra de carga
    const loadingBar = document.getElementById('loading-bar');

    if (shareButton) {
        shareButton.addEventListener('click', function() {
            // Obtiene la URL del video desde el atributo data-* del botón
            const videoUrl = this.dataset.videoUrl;

            // 1. Ocultar el botón y mostrar la barra de carga
            shareButton.style.display = 'none';
            loadingBar.style.display = 'block';

            // 2. Simular un proceso de "exportación" de 2 segundos.
            // Esto mejora la experiencia de usuario, haciendo parecer que algo está pasando.
            setTimeout(() => {
                // 3. Crear un enlace (<a>) en la memoria para iniciar la descarga
                const link = document.createElement('a');
                link.href = videoUrl;
                
                // El atributo 'download' le dice al navegador que descargue el archivo
                // en lugar de navegar a él. Le damos un nombre sugerido.
                link.download = 'video_memorial.mp4';
                
                // Añadimos el enlace al cuerpo del documento (es necesario en algunos navegadores)
                document.body.appendChild(link);
                
                // Simulamos un clic en el enlace
                link.click();
                
                // Removemos el enlace del cuerpo del documento
                document.body.removeChild(link);

                // 4. Ocultar la barra de carga y volver a mostrar el botón
                loadingBar.style.display = 'none';
                shareButton.style.display = 'inline-block';

            }, 2000); // 2000 milisegundos = 2 segundos
        });
    }
});
