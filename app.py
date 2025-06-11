import os
import threading
from queue import Queue
from datetime import datetime
import logging
import subprocess # Importar subprocess para ejecutar comandos FFmpeg
import shutil # Para eliminar directorios temporales
from io import BytesIO # Importar BytesIO para manejar el PDF en memoria

from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
import qrcode
from moviepy import ImageSequenceClip # Usar ImageSequenceClip en lugar de ImageClip y concatenate_videoclips
from PIL import Image # Importar la librería Pillow para manipulación de imágenes

# --- DEPENDENCIAS EXTERNAS IMPORTANTES ---
#
# Para que la generación de video funcione, este proyecto depende de software externo
# que debe estar instalado en el sistema donde se ejecuta la aplicación:
#
# 1. FFmpeg: Es OBLIGATORIO. MoviePy lo usa internamente para crear los videos.
#    - Instrucciones de instalación: https://www.geeksforgeforgeeks.org/how-to-install-ffmpeg-on-windows/
#    - Asegúrate de que el ejecutable de `ffmpeg` esté disponible en el PATH del sistema
#      para que MoviePy pueda encontrarlo automáticamente.
#
# 2. ImageMagick: Es MUY RECOMENDADO. MoviePy lo necesita para procesar imágenes
#    de forma robusta y renderizar texto. La falta de ImageMagick es una causa
#    muy común de errores inesperados con imágenes.
#    - Instrucciones de instalación: https://imagemagick.org/script/download.php
#
# ----------------------------------------------------------------------------------

# --- PARA GENERACIÓN DE PDF ---
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Image as ReportLabImage, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
# --- FIN PARA GENERACIÓN DE PDF ---

# --- CONFIGURACIÓN DE LOGGING ---
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# --- CONFIGURACIÓN DE LA APLICACIÓN FLASK ---
app = Flask(__name__)
app.config['SECRET_KEY'] = 'una-clave-secreta-muy-dificil-de-adivinar'

# Configuración de la base de datos SQLite
basedir = os.path.abspath(os.path.dirname(__file__))
instance_path = os.path.join(basedir, 'instance')
os.makedirs(instance_path, exist_ok=True)
# Configuración de la base de datos para leer desde Render
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    # Render usa 'postgres://' pero SQLAlchemy prefiere 'postgresql://'
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL or 'sqlite:///' + os.path.join(instance_path, 'database.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Configuración de carpetas para subidas de archivos
app.config['UPLOAD_FOLDER'] = os.path.join(basedir, 'static', 'uploads')
app.config['QR_CODE_FOLDER'] = os.path.join(basedir, 'static', 'qrcodes')
app.config['VIDEO_FOLDER'] = os.path.join(basedir, 'static', 'videos')
app.config['MUSIC_FOLDER'] = os.path.join(basedir, 'static', 'music')

# Asegurarse de que las carpetas de trabajo existan
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['QR_CODE_FOLDER'], exist_ok=True)
os.makedirs(app.config['VIDEO_FOLDER'], exist_ok=True)
os.makedirs(app.config['MUSIC_FOLDER'], exist_ok=True) # Asegurar que la carpeta de música exista

# Cola para manejar las solicitudes de generación de video de forma asíncrona
video_generation_queue = Queue()

db = SQLAlchemy(app)

# --- MODELO DE BASE DE DATOS ---
class Persona(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(150), nullable=False)
    fecha_nacimiento = db.Column(db.Date, nullable=False)
    fecha_muerte = db.Column(db.Date, nullable=False)
    qr_code_path = db.Column(db.String(200), nullable=True)
    images_uploaded = db.Column(db.Boolean, default=False)
    video_generated = db.Column(db.Boolean, default=False)
    video_path = db.Column(db.String(200), nullable=True)
    video_processing = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f'<Persona {self.nombre}>'

# --- LÓGICA DEL TRABAJADOR DE VIDEO (SEGUNDO PLANO) ---
def video_worker():
    """
    Función que se ejecuta en un hilo separado.
    Espera tareas en la cola y genera los videos uno por uno.
    """
    while True:
        app.logger.info("Trabajador de video esperando una nueva tarea...")
        person_id = video_generation_queue.get() 

        if person_id is None:
            app.logger.info("Señal de terminación recibida. Saliendo del trabajador de video.")
            break

        app.logger.info(f"Tarea recibida. Iniciando generación de video para la persona con ID: {person_id}")
        
        with app.app_context():
            persona = db.session.get(Persona, person_id)
            if not persona:
                app.logger.error(f"Error Crítico: Persona con ID {person_id} no encontrada en la base de datos.")
                video_generation_queue.task_done()
                continue
            
            # Definir variables fuera del try para poder limpiarlas en el finally
            video_sin_audio_filepath_abs = None
            video_final_filepath_abs = None
            temp_resized_folder = None # Inicializar para el bloque finally

            try:
                # --- LÓGICA PRINCIPAL DE CREACIÓN DE VIDEO ---
                image_folder = os.path.join(app.config['UPLOAD_FOLDER'], str(persona.id))
                
                if not os.path.exists(image_folder):
                    raise FileNotFoundError(f"La carpeta de imágenes no existe: {image_folder}")

                image_files = sorted([f for f in os.listdir(image_folder) if os.path.isfile(os.path.join(image_folder, f))])
                image_paths = [os.path.join(image_folder, img) for img in image_files]
                
                if not image_paths:
                    raise ValueError("No se encontraron imágenes para procesar.")

                duracion_por_imagen = 4 # segundos por imagen
                fps = 24 # frames por segundo

                # --- Redimensionar imágenes a un tamaño uniforme ---
                target_width = 1280
                target_height = 720
                target_resolution = (target_width, target_height)

                resized_image_paths = []
                # Crear una carpeta temporal para las imágenes redimensionadas dentro de la carpeta de la persona
                temp_resized_folder = os.path.join(image_folder, 'resized_temp') 
                os.makedirs(temp_resized_folder, exist_ok=True)

                app.logger.info(f"⚙️ Redimensionando imágenes a {target_width}x{target_height}...")
                for i, img_path in enumerate(image_paths):
                    try:
                        img = Image.open(img_path)
                        # Redimensionar la imagen. Esto puede cambiar la relación de aspecto.
                        # Image.LANCZOS es un filtro de alta calidad para redimensionar.
                        img_resized = img.resize(target_resolution, Image.LANCZOS)
                        
                        resized_img_filename = f"resized_{i}_{os.path.basename(img_path)}"
                        resized_img_path = os.path.join(temp_resized_folder, resized_img_filename)
                        img_resized.save(resized_img_path)
                        resized_image_paths.append(resized_img_path)
                    except Exception as e:
                        app.logger.error(f"Error al redimensionar la imagen {img_path}: {e}", exc_info=True)
                        # Si una imagen falla, la saltamos. Asegúrate de que aún haya suficientes imágenes.
                        continue

                if not resized_image_paths:
                    raise ValueError("No se redimensionaron imágenes exitosamente para procesar. Asegúrate de que las imágenes sean válidas.")
                
                # Nombres de archivos temporales y finales
                video_sin_audio_filename = f'temp_video_no_audio_{persona.id}.mp4'
                video_final_filename = f'memorial_{persona.id}_{int(datetime.now().timestamp())}.mp4'
                
                # Usar os.path.join solo para la ruta absoluta de guardado en el sistema de archivos
                video_sin_audio_filepath_abs = os.path.join(app.config['VIDEO_FOLDER'], video_sin_audio_filename)
                video_final_filepath_abs = os.path.join(app.config['VIDEO_FOLDER'], video_final_filename)

                # --- Crear video a partir de imágenes redimensionadas ---
                app.logger.info("⚙️ Creando clip de video a partir de imágenes redimensionadas...")
                # ImageSequenceClip toma una lista de rutas de imágenes y una lista de duraciones para cada imagen
                clip_imagenes = ImageSequenceClip(resized_image_paths, durations=[duracion_por_imagen] * len(resized_image_paths))
                clip_imagenes.write_videofile(video_sin_audio_filepath_abs, fps=fps, audio=False, logger=None)
                app.logger.info(f"Video sin audio creado: {video_sin_audio_filepath_abs}")
                clip_imagenes.close() # Liberar recursos del clip

                # --- Añadir audio con FFmpeg ---
                musica_fondo_path = os.path.join(app.config['MUSIC_FOLDER'], 'background_music.mp3') # Suponemos un nombre de archivo de música fijo
                usar_musica = os.path.exists(musica_fondo_path)

                if usar_musica:
                    app.logger.info("🔊 Añadiendo audio con FFmpeg...")
                    comando_combinar = [
                        'ffmpeg',
                        '-i', video_sin_audio_filepath_abs,
                        '-i', musica_fondo_path,
                        '-c:v', 'copy',          # Copiar video sin recodificar
                        '-c:a', 'aac',           # Codificar audio a AAC
                        '-strict', 'experimental', # Necesario para AAC en algunas versiones de FFmpeg
                        '-map', '0:v:0',        # Tomar stream de video del primer archivo (video sin audio)
                        '-map', '1:a:0',        # Tomar stream de audio del segundo archivo (música)
                        '-shortest',             # Ajustar a la duración más corta (video o audio)
                        video_final_filepath_abs
                    ]
                    
                    subprocess.run(comando_combinar, check=True)
                    app.logger.info(f"🎉 ¡Video con audio creado: {video_final_filepath_abs}!")
                else:
                    app.logger.warning(f"No se encontró el archivo de música: {musica_fondo_path}. Se copiará el video sin audio.")
                    # Si no hay música, simplemente copia el video sin audio al nombre final
                    os.rename(video_sin_audio_filepath_abs, video_final_filepath_abs)
                    app.logger.info(f"✅ Video copiado sin audio: {video_final_filepath_abs}")

                # Actualizar el registro en la base de datos
                # CRÍTICO: ALMACENAR LA RUTA RELATIVA CON BARRAS DIAGONALES PARA LAS URLs.
                persona.video_path = f'videos/{video_final_filename}' 
                persona.video_generated = True

                # --- TEMPORAL: Imprimir para depuración ---
                app.logger.debug(f"DEBUG: Guardando persona.video_path como: {persona.video_path}")
                # --- FIN TEMPORAL ---

            except subprocess.CalledProcessError as e:
                app.logger.error(f"FALLO la generación de video para la persona {person_id} debido a un error de FFmpeg: {e}", exc_info=True)
                # Limpiar el archivo final si se creó parcialmente
                if video_final_filepath_abs and os.path.exists(video_final_filepath_abs):
                    os.remove(video_final_filepath_abs)
            except Exception as e:
                app.logger.error(f"FALLO la generación de video para la persona {person_id}: {e}", exc_info=True)
            
            finally:
                # Limpiar archivo temporal sin audio si existe
                if video_sin_audio_filepath_abs and os.path.exists(video_sin_audio_filepath_abs):
                    os.remove(video_sin_audio_filepath_abs)
                
                # Limpiar la carpeta temporal de imágenes redimensionadas
                if temp_resized_folder and os.path.exists(temp_resized_folder):
                    try:
                        shutil.rmtree(temp_resized_folder)
                        app.logger.info(f"Carpeta temporal de imágenes redimensionadas eliminada: {temp_resized_folder}")
                    except Exception as e:
                        app.logger.error(f"Error al eliminar la carpeta temporal de imágenes redimensionadas {temp_resized_folder}: {e}", exc_info=True)

                # Marcar el proceso como finalizado (incluso si falló)
                persona.video_processing = False
                db.session.commit()
                app.logger.info(f"Proceso finalizado para la persona {person_id}. Actualizando base de datos.")
                video_generation_queue.task_done()


# --- INICIAR EL TRABAJADOR EN UN HILO SEPARADO ---
threading.Thread(target=video_worker, daemon=True).start()
app.logger.info("Trabajador de video iniciado en segundo plano.")

# --- RUTAS DE LA APLICACIÓN WEB ---

@app.route('/', methods=['GET', 'POST'])
def login():
    if 'logged_in' in session:
        return redirect(url_for('admin'))
        
    if request.method == 'POST':
        if request.form['username'] == 'admin' and request.form['password'] == 'admin':
            session['logged_in'] = True
            flash('Inicio de sesión exitoso.', 'success')
            return redirect(url_for('admin'))
        else:
            flash('Credenciales incorrectas. Inténtalo de nuevo.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    flash('Has cerrado sesión.', 'info')
    return redirect(url_for('login'))

@app.route('/admin')
def admin():
    if 'logged_in' not in session:
        flash('Debes iniciar sesión para ver esta página.', 'warning')
        return redirect(url_for('login'))
    
    search_query = request.args.get('search')
    if search_query:
        personas = Persona.query.filter(Persona.nombre.ilike(f'%{search_query}%')).order_by(Persona.id.desc()).all()
    else:
        personas = Persona.query.order_by(Persona.id.desc()).all()
    
    return render_template('admin.html', personas=personas, search_query=search_query)

@app.route('/add_person', methods=['POST'])
def add_person():
    if 'logged_in' not in session:
        return redirect(url_for('login'))

    try:
        nueva_persona = Persona(
            nombre=request.form['nombre'],
            fecha_nacimiento=datetime.strptime(request.form['fecha_nacimiento'], '%Y-%m-%d').date(),
            fecha_muerte=datetime.strptime(request.form['fecha_muerte'], '%Y-%m-%d').date()
        )
        db.session.add(nueva_persona)
        db.session.commit()

        qr_path_rel = generate_qr_code(nueva_persona.id)
        nueva_persona.qr_code_path = qr_path_rel
        db.session.commit()

        flash(f'Persona "{nueva_persona.nombre}" añadida y QR generado.', 'success')
    except Exception as e:
        flash(f'Error al añadir persona: {e}', 'danger')
    
    return redirect(url_for('admin'))

@app.route('/view/<int:person_id>')
def view_person(person_id):
    persona = db.session.get(Persona, person_id)
    if not persona:
        return "No encontrado", 404
        
    image_files = []
    if persona.images_uploaded:
        person_upload_folder = os.path.join(app.config['UPLOAD_FOLDER'], str(persona.id))
        if os.path.exists(person_upload_folder):
            try:
                image_files = sorted(os.listdir(person_upload_folder))
            except OSError:
                image_files = []

    return render_template('view_person.html', persona=persona, image_files=image_files)

@app.route('/upload_images/<int:person_id>', methods=['POST'])
def upload_images(person_id):
    persona = db.session.get(Persona, person_id)
    if not persona:
        return "No encontrado", 404
    
    if persona.images_uploaded:
        flash('Las imágenes para esta persona ya han sido subidas.', 'warning')
        return redirect(url_for('view_person', person_id=person_id))

    uploaded_files = request.files.getlist('images')
    if not (3 <= len(uploaded_files) <= 10):
        flash('Debes subir entre 3 y 10 imágenes.', 'danger')
        return redirect(url_for('view_person', person_id=person_id))

    person_upload_folder = os.path.join(app.config['UPLOAD_FOLDER'], str(person_id))
    os.makedirs(person_upload_folder, exist_ok=True)

    for file in uploaded_files:
        if file and file.filename != '':
            filename = secure_filename(file.filename)
            file.save(os.path.join(person_upload_folder, filename))

    persona.images_uploaded = True
    db.session.commit()

    flash('Imágenes subidas. Ya puedes generar el video memorial.', 'success')
    return redirect(url_for('view_person', person_id=person_id))

@app.route('/generate_video/<int:person_id>', methods=['POST'])
def generate_video(person_id):
    persona = db.session.get(Persona, person_id)
    if not persona:
        return "No encontrado", 404
    
    if not persona.images_uploaded or persona.video_generated or persona.video_processing:
        flash('Acción no permitida: el video no se puede generar en este estado.', 'warning')
        return redirect(url_for('view_person', person_id=person_id))

    persona.video_processing = True
    db.session.commit()

    video_generation_queue.put(person_id)
    app.logger.info(f"Persona con ID {person_id} añadida a la cola de generación de video.")

    flash('El video se está generando. Esta operación puede tardar unos minutos.', 'info')
    return redirect(url_for('view_person', person_id=person_id))

@app.route('/export_pdf')
def export_pdf():
    if 'logged_in' not in session:
        flash('Debes iniciar sesión para exportar.', 'warning')
        return redirect(url_for('login'))

    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()

    story = []

    # Title
    story.append(Paragraph("Registro de Personas", styles['h1']))
    story.append(Spacer(1, 0.2 * inch))

    # Table Header
    data = [["ID", "Nombre", "Nacimiento", "Fallecimiento", "Estado", "Código QR"]]

    # Table Rows
    # Export all records, not just filtered ones, for the PDF export
    all_personas = Persona.query.order_by(Persona.id.asc()).all() 
    for persona in all_personas:
        qr_image_element = ""
        if persona.qr_code_path:
            # Construct the absolute path to the QR code image
            qr_full_path = os.path.join(app.root_path, 'static', persona.qr_code_path)
            if os.path.exists(qr_full_path):
                # ReportLabImage takes width and height. Adjust as needed.
                qr_image_element = ReportLabImage(qr_full_path, width=50, height=50) 
            else:
                qr_image_element = "QR Missing"
        else:
            qr_image_element = "No generado"

        status = ""
        if persona.video_generated:
            status = "Video Generado"
        elif persona.images_uploaded:
            status = "Imágenes Subidas"
        else:
            status = "Sin Imágenes"

        data.append([
            str(persona.id),
            persona.nombre,
            persona.fecha_nacimiento.strftime('%d/%m/%Y'),
            persona.fecha_muerte.strftime('%d/%m/%Y'),
            status,
            qr_image_element
        ])

    table = Table(data)

    # Table Style
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#6c757d')), # Bootstrap secondary color
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#f8f9fa')), # Bootstrap light color
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)

    return send_file(buffer, as_attachment=True, download_name='registros_personas.pdf', mimetype='application/pdf')


# --- FUNCIONES AUXILIARES ---
def generate_qr_code(person_id):
    """Genera un QR que apunta a la página de visualización y devuelve la ruta relativa."""
    view_url = url_for('view_person', person_id=person_id, _external=True)
    qr_filename = f'qr_{person_id}.png'
    qr_path_abs = os.path.join(app.config['QR_CODE_FOLDER'], qr_filename)
    
    img = qrcode.make(view_url)
    img.save(qr_path_abs)
    
    # CRÍTICO: ALMACENAR LA RUTA RELATIVA CON BARRAS DIAGONALES PARA LAS URLs.
    # La ruta devuelta debe ser relativa a la carpeta 'static'
    return f'qrcodes/{qr_filename}'

if __name__ == '__main__':
    with app.app_context():
        # --- CRÍTICO: ELIMINAR LA BASE DE DATOS EXISTENTE PARA ASEGURAR RUTAS LIMPIAS ---
        # ATENCIÓN: Esto borrará todos tus datos. Comenta esta línea si no quieres perderlos
        # y prefieres actualizar los registros manualmente si la base de datos es persistente.
        # db.drop_all() # Descomenta para borrar todos los datos
        db.create_all()
    # use_reloader=False es importante para evitar que el hilo de fondo se inicie dos veces
    app.run(debug=True, host='0.0.0.0', port=5001, use_reloader=False)