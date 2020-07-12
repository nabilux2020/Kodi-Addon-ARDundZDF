# -*- coding: utf-8 -*-
###################################################################################################
#							 epgRecord.py - Teil von Kodi-Addon-ARDundZDF
#							Aufnahmefunktionen für Sendungen in EPG-Menüs
#							Verzicht auf sqlite3, Verwaltung + Steuerung
#									nur mittels Dateifunktionen
#
####################################################################################################
#	01.07.2020 Start
#	Stand 12.07.2020

# Python3-Kompatibilität:
from __future__ import absolute_import		# sucht erst top-level statt im akt. Verz. 
from __future__ import division				# // -> int, / -> float
from __future__ import print_function		# PYTHON2-Statement -> Funktion
from kodi_six import xbmc, xbmcaddon, xbmcplugin, xbmcgui, xbmcvfs

# o. Auswirkung auf die unicode-Strings in PYTHON3:
from kodi_six.utils import py2_encode, py2_decode

import os, sys, subprocess, signal 
PYTHON2 = sys.version_info.major == 2
PYTHON3 = sys.version_info.major == 3
if PYTHON2:
	from urllib import quote, unquote, quote_plus, unquote_plus, urlencode, urlretrieve
	from urllib2 import Request, urlopen, URLError 
	from urlparse import urljoin, urlparse, urlunparse, urlsplit, parse_qs
elif PYTHON3:
	from urllib.parse import quote, unquote, quote_plus, unquote_plus, urlencode, urljoin, urlparse, urlunparse, urlsplit, parse_qs
	from urllib.request import Request, urlopen, urlretrieve
	from urllib.error import URLError

import time, datetime
from threading import Thread	

from resources.lib.util import *
import resources.lib.EPG as EPG
#from ardundzdf import MakeDetailText, LiveRecord


ADDON_ID      	= 'plugin.video.ardundzdf'
SETTINGS 		= xbmcaddon.Addon(id=ADDON_ID)
ADDON_PATH    	= SETTINGS.getAddonInfo('path')
ADDON_NAME    	= SETTINGS.getAddonInfo('name')
USERDATA		= xbmc.translatePath("special://userdata")
ADDON_DATA		= os.path.join("%sardundzdf_data") % USERDATA

if 	check_AddonXml('"xbmc.python" version="3.0.0"'):
	ADDON_DATA	= os.path.join("%s", "%s", "%s") % (USERDATA, "addon_data", ADDON_ID)
DICTSTORE 		= os.path.join("%s/Dict") % ADDON_DATA

JOBFILE			= os.path.join("%s/jobliste.xml") % ADDON_DATA
JOBFILE_LOCK	= os.path.join("%s/jobliste.lck") % ADDON_DATA		# Lockfile für Jobliste
JOB_STOP		= os.path.join("%s/job_stop") % ADDON_DATA			# Stopfile für JobMonitor
MONITOR_ALIVE 	= os.path.join("%s/monitor_alive") % ADDON_DATA		# Lebendsignal für JobMonitor (leer, mtime-Abgleich)

JOBLINE_TEMPL	= "<startend>%s</startend><title>%s</title><descr>%s</descr><sender>%s</sender><url>%s</url><status>%s</status><pid>%s</pid>"
JOB_TEMPL		= "<job>%s</job>"
JOBLIST_TEMPL	= "<jobliste>\n%s\n</jobliste>"
JOBDELAY 		= 60	# Sek.=1 Minute

ICON_DOWNL_DIR	= "icon-downl-dir.png"
MSG_ICON 		= R("icon-record.png")

##################################################################
#---------------------------------------------------------------- 
# Problem: Verwendung der Settings als Statuserkennung unzuverlässig -
#	daher Verwendung von JOB_STOP + MONITOR_ALIVE ähnlich FILTER_SET
#	in FilterTools(). 
#	06.07.2020 zusätzl. direkter Zugriff auf settings.xml via
#		get_Setting (util).
# Abbruch Kodi: 	abortRequested 
# Abbruch intern: 	1. Datei JOB_STOP (fehlendes JOBFILE: kein Abbruch)
#					2. Setting pref_epgRecord (direkt)						
# Lock: wegen mögl. konkur. Zugriffe auf die Jobdatei wird eine Lock-
#	datei verwendet (JobMain, Monitor)
#
def JobMonitor():
	PLog("JobMonitor:")
	pre_rec  = SETTINGS.getSetting('pref_pre_rec')			# Vorlauf (Bsp. 00:15:00 = 15 Minuten)
	post_rec = SETTINGS.getSetting('pref_post_rec')			# Nachlauf (dto.)
	pre_rec = re.search('= (\d+) Min', pre_rec).group(1)
	post_rec = re.search('= (\d+) Min', post_rec).group(1)
		
	if os.path.exists(JOB_STOP):							# Ruine?		
		os.remove(JOB_STOP)	
	
	old_age=0
	if os.path.exists(JOBFILE):	
		jobs = ReadJobs()										# s. util
		old_age = os.stat(JOBFILE).st_mtime						# Abgleich in Monitor (z.Z. n.verw.)
	else:
		jobs = []
	PLog("JOBFILE_old_age: %d" % old_age)
	
	i=0
	monitor = xbmc.Monitor()
	while not monitor.abortRequested():
		i=i+1
		PLog("Monitor_Lauf: %d" % i)
		
		if get_Setting('pref_epgRecord') == 'false':		# direkter Zugriff (s.o.)
			PLog("Monitor: pref_epgRecord false - stop")
			xbmcgui.Dialog().notification("Aufnahme-Monitor:","gestoppt",MSG_ICON,3000)
			break
		
		open(MONITOR_ALIVE, 'w').close()					# Lebendsignal - Abgleich mtime in JobMain

		xbmc.sleep(JOBDELAY * 1000)							# Pause
		# xbmc.sleep(2000)	# Debug
		if os.path.exists(JOB_STOP): 
			PLog("Monitor: JOB_STOP gefunden - stop")
			xbmcgui.Dialog().notification("Aufnahme-Monitor:","gestoppt",MSG_ICON,3000)
			break
				
		if os.path.exists(JOBFILE):							# bei jedem Durchgang neu einlesen
			jobs = ReadJobs()
			
		now = EPG.get_unixtime(onlynow=True)
		now = int(now)
		now_human = date_human("%Y.%m.%d_%H:%M:%S", now='')			# Debug	
		
		#---------------------------------------------------		# Schleife Jobliste		
		PLog("scan_jobs:")
		newjob_list = []; cnt=0; job_changed = False				# newjob_list: Liste nach Änderungen
		for cnt in range(len(jobs)):
			myjob = jobs[cnt]
			PLog(myjob[:80])			
			status 	= stringextract('<status>', '</status>', myjob)
			PLog("Job %d status: %s" % (cnt+1, status))			
												
			start_end 	= stringextract('<startend>', '</startend>', myjob)
			start, end = start_end.split('|')						# 1593627300|1593633300					
			start 	= int(start) - int(pre_rec) * 60				# Vorlauf (Min -> Sek) abziehen
			end 	= int(end) + int(post_rec) * 60					# Nachlauf (Min -> Sek) aufschlagen 
			start_human = date_human("%Y.%m.%d_%H:%M:%S", now=start)
			mydate = date_human("%Y%m%d_%H%M%S", now=start)			# Zeitstempel für titel in LiveRecord	
			end_human= date_human("%Y.%m.%d_%H:%M:%S", now=end)			

			duration = end - start									# in Sekunden für ffmpeg
			diff = start - now
			vorz=''
			if diff < 0: 
				vorz = "minus "
			diff = seconds_translate(diff)	
			
			laenge = ""												# entfällt hier
			# PLog("now %s, start %s, end %s" % (now, start, end))  # Debug
			PLog("now %s, start %s, end %s, start-now: %s" % (now_human, start_human, end_human, diff))
			
			#---------------------------------------------------	# 1 Job -> Aufnahme		
			if (now >= start and now <= end) and status == 'waiting':	# Job ist aufnahmereif
				PLog("Job ready: " + start_end)	
				url = stringextract('<url>', '</url>', myjob)
				sender = stringextract('<sender>', '</sender>', myjob)
				title = stringextract('<title>', '</title>', myjob)
				title = "%s: %s" % (sender, title)					# Titel: Sender + Sendung
				descr = stringextract('<descr>', '</descr>', myjob)
				
				started = date_human("%Y.%M.D_%H:%M:%S", now=start)
				dfname = make_filenames(title.strip()) 				# Name aus Titel
																	# Textdatei
				txttitle = "%s_%s" % (mydate, dfname)				# Zeitstempel wie inLiveRecord  
				detailtxt = MakeDetailText(title=txttitle,thumb='',quality='Livestream',
					summary=descr,tagline='',url=url)
				detailtxt=py2_encode(detailtxt); txttitle=py2_encode(txttitle);		
				storetxt = 'Details zur Aufnahme ' +  txttitle + ':\r\n\r\n' + detailtxt
				dest_path = SETTINGS.getSetting('pref_download_path')
				textfile = txttitle + '.txt'
											
				dfname = dfname + '.mp4'							# LiveRecord ergänzt Zeitstempel 
				textfile = py2_encode(textfile)	 
				pathtextfile = os.path.join(dest_path, textfile)
				storetxt = py2_encode(storetxt)
				RSave(pathtextfile, storetxt)						# Begleitext speichern				
						
				# duration = "00:00:10"	# Debug: 10 Sec.
				PLog("Satz:")
				PLog(url); PLog(title); PLog(duration); PLog(detailtxt); PLog(started);
								
				myjob = myjob.replace('<status>waiting', '<status>gestartet')
				PLog("Job %d started" % cnt)
				job_changed = True
				PIDffmpeg = LiveRecord(url, title, duration, laenge='', epgJob=mydate) # Aufnehmen
				myjob = myjob.replace('<pid></pid>', '<pid>%s</pid>' % PIDffmpeg)

			#---------------------------------------------------	# Job zurück in Liste		
			jobs[cnt] = JOB_TEMPL % myjob							# Job -> Listenelement
			PLog("Job %d loopend: %s" % (cnt+1, jobs[cnt][:40]))
			cnt=cnt+1												# und nächster Job
			
		#---------------------------------------------------		# Jobliste speichern, falls geändert	
		if job_changed:											
			newjobs = "\n".join(jobs)					
			page = JOBLIST_TEMPL % newjobs							# Jobliste -> Marker
			page = py2_encode(page)
			PLog(u"geänderte Jobliste speichern:")
			PLog(page[:80])
			open(JOBFILE_LOCK, 'w').close()							# Lock ein				
			err_msg = RSave(JOBFILE, page)							# Jobliste speichern
			if os.path.exists(JOBFILE_LOCK):						# Lock aus
				os.remove(JOBFILE_LOCK)	
						
	if os.path.exists(MONITOR_ALIVE):								# Aufräumen nach Monitor-Ende
		os.remove(MONITOR_ALIVE)
		
	return

######################################################################## 
# 
# Aufrufer:
#	action init: bei jedem Start ardundzdf.py (bei Setting pref_epgRecord)
# 	action stop: DownloadTools
#	action setjob: ProgramRecord
# 
# Checks auf ffmpegCall + download_path in ProgramRecord
# Problem : bei Abstürzen (network error) kann das Lebendsignal
#	MONITOR_ALIVE als Ruine stehenbleiben. Lösung: falls mtime
#	mehr als JOBDELAY zurückliegt, gilt Monitor als tot - init ist 
#	wieder möglich.
# 	threading.enumerate() hier nicht geeignet (iefert nur MainThread)
#	
def JobMain(action, start_end='', title='', descr='',  sender='', url='', setSetting=''):
	PLog("JobMain:")
	PLog(action); PLog(sender); PLog(title);  
	PLog(descr); PLog(start_end);	
	
	# mythreads = threading.enumerate()								# liefert in Kodi nur MainThread
	status = os.path.exists(MONITOR_ALIVE)
	PLog("MONITOR_ALIVE pre: " + str(status))						# Eingangs-Status	
	now = EPG.get_unixtime(onlynow=True)				
	
	#------------------------
	if action == 'init':											# bei jedem Start ardundzdf.py
		if setSetting:												# Aufruf: DownloadTools
			SETTINGS.setSetting('pref_epgRecord', 'true')
			xbmcgui.Dialog().notification("Aufnahme-Monitor:","Start veranlasst",MSG_ICON,3000) 
			xbmc.executebuiltin('Container.Refresh')
			return
			
		if os.path.exists(MONITOR_ALIVE):							# check Lebendsignal
			mtime = os.stat(MONITOR_ALIVE).st_mtime
			diff = int(now) - mtime
			PLog("now: %s, mtime: %d, diff: %d" % (now, mtime, diff))

			if diff > JOBDELAY:										# Monitor tot? 
				PLog("alive_veraltet: force init")					# abhängig von JOBFILE, s.u.
				os.remove(MONITOR_ALIVE)
			else:
				PLog("alive_aktuell: return")		
				return
		else:
			PLog("alive_fehlt: force init")							# dto.
				
		if check_file(JOBFILE) == False:							# JOBFILE leer/fehlt - kein Hindernis
			PLog("Aufnahmeliste leer")
			
		if os.path.exists(MONITOR_ALIVE) == False:					# JobMonitor läuft bereits?
			bg_thread = Thread(target=JobMonitor,					# sonst Thread JobMonitor starten
				args=())
			bg_thread.start()
			xbmcgui.Dialog().notification("Aufnahme-Monitor:","gestartet",MSG_ICON,3000)
		else:
			PLog("running - skip init")
		return		
					
	#------------------------
	if action == 'stop':											# DownloadTools <-
		jobs = ReadJobs()											# s. util
		now = EPG.get_unixtime(onlynow=True)
		job_active = False
		PLog('Mark0')
		for job in jobs:
			start_end 	= stringextract('<startend>', '</startend>', job)
			start, end = start_end.split('|')						# 1593627300|1593633300					
			if int(start) > int(now):
				job_active = True
				break
		PLog('Mark1')
		if job_active:
			title = 'Aufnahme-Monitor stoppen'					
			msg1 = "Mindestens ein Aufnahmejob ist noch aktiv!"
			msg2 = "Aufnahme-Monitor trotzdem stoppen?"
			ret = MyDialog(msg1=msg1, msg2=msg2, msg3='', ok=False, cancel='Abbruch', yes='JA', heading=title)
			if ret !=1:
				return
			
		open(JOB_STOP, 'w').close()	# STOPFILE anlegen				
		SETTINGS.setSetting('pref_epgRecord', 'false')				# Setting muss manuell wieder eingeschaltet werden
		PLog("JOB_STOP set")										# Status
		xbmc.executebuiltin('Container.Refresh')
		xbmcgui.Dialog().notification("Aufnahme-Monitor:","Stop veranlasst",MSG_ICON,3000) # Notification im Monitor
		# test_jobs		# Debug
		return
		
	#------------------------
	if action == 'setjob':							# neuen Job an Aufnahmeliste anhängen + Bereinigung: Doppler
													# 	verhindern, Einträge auf pref_max_reclist beschränken
		status = 'waiting'											# -> <status>,  JobMonitor aktualisiert 
		title = cleanmark(title)									# Farbe/fett aus ProgramRecord
		pid = ''									# nimmt im Monitor PIDffmpeg auf
		job_line = JOBLINE_TEMPL % (start_end,title,descr,sender,url,status,pid)
		new_job = JOB_TEMPL % job_line
		PLog(new_job[:80])
		jobs = ReadJobs()											# s. util
	
		job_list = []; cnt=0										# Neubau Jobliste
		# Kontrolle erweitern, falls startend + sender nicht eindeutig genug:
		for job in jobs:
			list_startend 	= stringextract('<startend>', '</startend>', job)
			list_sender 	= stringextract('<sender>', '</sender>', job)
			if list_startend == start_end and list_sender == sender:	# Kontrolle Doppler
				msg1 = "%s: %s\nStart/Ende (Unix-Format): %s" % (sender, title, start_end)
				msg2 = "Sendung ist bereits in der Jobliste - Abbruch"
				MyDialog(msg1, msg2, '')
				PLog("%s\n%s" % (msg1, msg2))
				return												# alte Liste unverändert
			else:
				job_list.append(JOB_TEMPL % job)					# job -> Marker
				cnt=cnt+1
		
		new_job = py2_encode(new_job)											
		job_list.append(new_job)									# neuen Job anhängen
		maxlen = int(SETTINGS.getSetting('pref_max_reclist'))
		if len(job_list) > maxlen: 
			while len(job_list) > maxlen: 
				del job_list[0]											# 1. Satz entf.
				PLog('%d/%d, Job entfernt: %s' % (maxlen, len(job_list), job_list[0]))
		jobs = "\n".join(job_list)					
		page = JOBLIST_TEMPL % jobs									# Jobliste -> Marker
		page = py2_encode(page)
		PLog(page[:80])
			
		open(JOBFILE_LOCK, 'w').close()								# Lock ein				
		err_msg = RSave(JOBFILE, page)								# Jobliste speichern
		if os.path.exists(JOBFILE_LOCK):							# Lock aus
			os.remove(JOBFILE_LOCK)	
		
		xbmcgui.Dialog().notification("Aufnahme-Monitor:", "Job hinzugefügt",MSG_ICON,3000)
		
		if os.path.exists(MONITOR_ALIVE) == False:					# JobMonitor läuft bereits?
			bg_thread = Thread(target=JobMonitor,					# sonst Thread JobMonitor starten
				args=())
			bg_thread.start()
		return
	#------------------------
	if action == 'listJobs':										# Liste, Job-Status, Jobs löschen
		JobListe()
		return	

	#------------------------
	if action == 'deljob':											# Job aus Aufnahmeliste entfernen
		JobRemove()
		return	
		
	#------------------------
	if action == 'test_jobs':										# Testfunktion
		test_jobs()
		return	

##################################################################
#---------------------------------------------------------------- 
def JobListe():														# Liste, Job-Status, Jobs löschen
	PLog("JobListe:")
	
	li = xbmcgui.ListItem()
	li = home(li, ID=NAME)				# Home-Button
	
	if os.path.exists(JOBFILE):	
		jobs = ReadJobs()											# s. util
		if len(jobs) == 0:
			xbmcgui.Dialog().notification("Jobliste:", "keine Aufnahme-Jobs vorhanden",MSG_ICON,3000)	
	else:
			xbmcgui.Dialog().notification("Jobliste:", "nicht gefunden",MSG_ICON,3000)	
	
	now = EPG.get_unixtime(onlynow=True)
	now = int(now)
	now_human = date_human("%d.%m.%Y, %H:%M", now='')
	pre_rec  = SETTINGS.getSetting('pref_pre_rec')			# Vorlauf (Bsp. 00:15:00 = 15 Minuten)
	post_rec = SETTINGS.getSetting('pref_post_rec')			# Nachlauf (dto.)
	pre_rec = re.search('= (\d+) Min', pre_rec).group(1)
	post_rec = re.search('= (\d+) Min', post_rec).group(1)
	anz_jobs = len(jobs)
	
	for cnt in range(len(jobs)):
			myjob = jobs[cnt]
			PLog(myjob[:80])			
			status 	= stringextract('<status>', '</status>', myjob)
			status_real = status									# wird aktualisiert s.u.
			PLog("Job %d status: %s" % (cnt+1, status))			
																		
			start_end	= stringextract('<startend>', '</startend>', myjob)
			start, end 	= start_end.split('|')						# 1593627300|1593633300	
			start = int(start); end = int(end)				
			start 		= int(start) - int(pre_rec) * 60			# Vorlauf (Min -> Sek) abziehen
			end 		= int(end) + int(post_rec) * 60				# Nachlauf (Min -> Sek) aufschlagen 
			mydate = date_human("%Y%m%d_%H%M%S", now=start)			# Zeitstempel für titel in LiveRecord	
			start_human = date_human("%d.%m.%Y, %H:%M", now=start)
			end_human = date_human("%d.%m.%Y, %H:%M", now=end)
			
			pid = stringextract('<pid>', '</pid>', myjob)
			title = stringextract('<title>', '</title>', myjob)
			job_title = title										# für Abgleich in JobRemove
			sender = stringextract('<sender>', '</sender>', myjob)
			dfname = "%s: %s" % (sender, title)						# Titel: Sender + Sendung (mit Mark.)
			dfname = make_filenames(dfname.strip()) + ".mp4"		# Name aus Titel
			dfname = "%s_%s" % (mydate, dfname)						# wie LiveRecord
			dest_path = SETTINGS.getSetting('pref_download_path')
			video =  os.path.join(dest_path, dfname)
			PLog("video: " + video)
			status_add = ""
			if os.path.exists(video):
				status_add = " | Video vorhanden: %s.." % dfname[:28] 	# für Infobereich begrenzen
			else:
				status_add = " | Video fehlt: %s.." % dfname[:28]		# dto.
			
			title = cleanmark(title)
			title = title.replace("JETZT: ", '')
			
			img = MSG_ICON										# rot
			PLog(" end %s, now %s" % (end, now))
			job_active = False
			if end < now:										# abgelaufen
				img = R("icon-record-grey.png")					# grau
				if status == "gestartet":
					status_real = "[B] Jobstatus: Aufnahme wurde gestartet [/B]" + status_add
				else:
					status_real = "[B] Jobstatus: Aufnahme wurde nicht gestartet [/B] - Ursache nicht bekannt"
			else:												# noch aktiv
				img = MSG_ICON									# grau
				job_active = True
				status_real = "Aufnahme geplant: %s" % start_human
				
			label = u'Job löschen: %s'	% title 				
			tag = u'Start: %s, Ende: %s' % (start_human, end_human)
			tag = u'%s\n%s' % (tag, status_real)
			
			max_reclist = SETTINGS.getSetting('pref_max_reclist')
			summ = u'[B]Anzahl Jobs[/B] in der Aufnahmeliste: %s' % (anz_jobs)
			summ = u"%s\n[B]Settings[/B]:\n[B]max. Größe der Aufnahmeliste:[/B] %s Jobs," % (summ, max_reclist)
			summ = u"%s[B]Vorlauf:[/B] %s Min., [B]Nachlauf:[/B] %s Min." % (summ, pre_rec, post_rec)
			fparams="&fparams={'sender':'%s','job_title':'%s','start_end':'%s','job_active':'%s','pid':'%s'}" %\
				(sender, job_title, start_end, job_active, pid)
			addDir(li=li, label=label, action="dirList", dirID="resources.lib.epgRecord.JobRemove", fanart=R(ICON_DOWNL_DIR), 
				thumb=img, fparams=fparams, tagline=tag, summary=summ)
	
	xbmcplugin.endOfDirectory(HANDLE, cacheToDisc=False)
	
#----------------------------------------------------------------
# Aufrufer: JobListe
# job_title: unbehandelt, mit ev. Mark.
# title + start_end: eindeutige ID
#
def JobRemove(sender, job_title, start_end, job_active, pid):
	PLog("JobRemove:")
	
	msg1 = "%s: %s" % (sender, job_title)
	heading = u"Job aus Aufnahmeliste löschen"
	if job_active == 'True':
		msg2 = u"Job (PID %s) tatsächlich abbrechen und löschen?" % pid
		heading = "aktiven (!) %s!" % heading
		icon = MSG_ICON
	else:
		msg2 = u"Job tatsächlich löschen?" 
		heading = "abgelaufenen %s" % heading
		icon = R("icon-record-grey.png")
		
	ret = MyDialog(msg1=msg1, msg2=msg2, msg3='', ok=False, cancel='Abbruch', yes='JA', heading=heading)
	if ret !=1:
		return
	
	if job_active == 'True':
		os.kill(int(pid), signal.SIGTERM)						# auch Windows10 OK (aber Teilvideo beschäd.)
		PLog("kill_pid:  %s" % str(pid))
	
	jobs = ReadJobs()											# s. util
	newjob_list = []; 											# newjob_list: Liste nach Änderungen
	for job in jobs:
		my_start_end 	= stringextract('<startend>', '</startend>', job)
		my_title 	= stringextract('<title>', '</title>', job)
		if start_end == my_start_end and job_title == my_title: # skip=delete
			continue
		newjob_list.append(JOB_TEMPL % job)						# job -> Marker
		
	PLog(len(jobs))			
	jobs = "\n".join(newjob_list)
	page = JOBLIST_TEMPL % jobs									# Jobliste -> Marker
	page = py2_encode(page)
	if doLock(JOBFILE_LOCK):									
		err_msg = RSave(JOBFILE, page)							# Jobliste speichern
	doLock(JOBFILE_LOCK, remove=True)
	if err_msg == '':
		xbmcgui.Dialog().notification("Aufnahmeliste:",u"Job gelöscht",icon,3000) # Notification im Monitor
	else:
		xbmcgui.Dialog().notification("Aufnahmeliste:",u"Problem beim Speichern",icon,3000) # Notification im Monitor
	return
	
#---------------------------------------------------------------- 
# simpler Lock-Mechanismus
#	Aufrufer: 	1. checkLock (remove=False)
#				2. Datei speichern
#				3. checkLock (remove=True)
#	checkLock sorgt für Verzögerung (maxLockloops),
#		räumt eine ev. Ruine ab + setzt Lockfile 
#	mit Param. remove entfernt checkLock das Lock
#		sofort (i.d.R. nach dem Speichern)
# Bei Bedarf mit Modul Lockfile erweitern:
#	https://pypi.org/project/lockfile/
#
def doLock(lockfile, remove=False):
	PLog("doLock: " + str(remove))
	maxloops	= 10				# 1 sec bei 10 x xbmc.sleep(100)	

	if remove:
		if os.path.exists(lockfile):
			os.remove(lockfile)
			return True
			
	while os.path.exists(lockfile):	
		i=i+1
		if i >= maxloops:		# Lock brechen, vermutl. Ruine
			os.remove(lockfile)
			PLog("doLock_break: " + lockfile)
			break
		xbmc.sleep(100)	
	
	try:							# Lock setzen
		open(JOBFILE_LOCK, 'w').close()
	except Exception as exception:
		msg = str(exception)
		PLog('doLock_Exception: ' + msg)
		return False
			
	return True
#---------------------------------------------------------------- 
# gibt Unixzeit im lesbaren Format myformat zurück.
#	Formate s. https://strftime.org/
# falls now leer, wird die akt. Zeit ermittelt (lokal)
#
def date_human(myformat, now=''):
	PLog("date_human:")

	if now == '':
		now = EPG.get_unixtime(onlynow=True)
	now = int(now)
	s = datetime.datetime.fromtimestamp(now)
	date_human = s.strftime(myformat)	
	return date_human
	
##################################################################
#---------------------------------------------------------------- 
# nur Debug: Jobliste mit ausgewähltenJobs, Startzeit: jetzt + 1 Min.,
#		Aufnahmezeit 10 Sec. (Endzeit: Startzeit + 10 Sec.)
#		Ablage: direkt als JOBFILE nach ADDON_DATA (ohne Warnung!)
#				
def test_jobs():	
	PLog("test_jobs:")
	# "Das Erste|https://mcdn.daserste.de/daserste/de/master.m3u8"  - funkt. nicht
	mylist = [ 		"Das Erste|https://derste247livede.akamaized.net/hls/live/658317/daserste_de/profile1/1.m3u8",
					"BR Fernsehen-Nord|http://brlive-lh.akamaihd.net/i/bfsnord_germany@119898/master.m3u8", 
					"hr-fernsehen|https://hrlive1-lh.akamaihd.net/i/hr_fernsehen@75910/master.m3u8",
					"MDR Sachsen|https://mdrsnhls-lh.akamaihd.net/i/livetvmdrsachsen_de@513998/master.m3u8",
					"Radio Bremen|https://rbfs-lh.akamaihd.net/i/rb_fs@438960/master.m3u8", 
					"NDR Niedersachsen|https://ndrfs-lh.akamaihd.net/i/ndrfs_nds@430233/master.m3u8", 
					"NDR Hamburg|https://ndrfs-lh.akamaihd.net/i/ndrfs_hh@430231/master.m3u8", 
					"SR Fernsehen|https://srlive24-lh.akamaihd.net/i/sr_universal02@107595/master.m3u8",
					"Tagesschau24|http://tagesschau-lh.akamaihd.net/i/tagesschau_1@119231/master.m3u8",
					"Deutsche Welle|https://dwstream72-lh.akamaihd.net/i/dwstream72_live@123556/master.m3u8", 
					"One|http://onelivestream-lh.akamaihd.net/i/one_livestream@568814/master.m3u8",
				]
	add_sec = 10
	
	now = EPG.get_unixtime(onlynow=True)
	now = int(now)
	end = now + 2*add_sec
	now = now + add_sec

	job_list = []; i=0
	for item in mylist:
		i=i+1
		sender, url = item.split('|') 
		PLog("job %s" % str(i))
		end = now + 2*add_sec
		now = now + add_sec
		title = "Testjob %s" % str(i)
		start_end = "%s|%s" % (str(now), str(end))
		descr = u"Debug: Jobliste mit ausgewählten Jobs, Startzeit: jetzt + 10 Sec., Aufnahmezeit 10 Sec. (Endzeit: Startzeit + 10 Sec.)"
		status = "waiting"
		pid = ''
		
		job = JOBLINE_TEMPL % (start_end,title,descr,sender,url,status,pid)
		job_list.append(JOB_TEMPL % job)					# job -> Marker
	
	jobs = "\n".join(job_list)	
	page = JOBLIST_TEMPL % jobs								# Jobliste -> Marker
	page = py2_encode(page)
	PLog(page[:80])
	
	err_msg = RSave(JOBFILE, page)							# Jobliste -> Livesystem
	return
	
	
