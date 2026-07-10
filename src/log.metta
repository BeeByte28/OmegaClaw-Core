(= (log $type $module $msg)
   (case $type 
      ((INFO (py-call (logger.log_info $msg $module)))
      (DEBUG (py-call (logger.log_debug $msg $module)))
      (WARNING (py-call (logger.log_warning $msg $module)))
      (ERROR (py-call (logger.log_error $msg $module)))
      ($else ERROR-LOGGER-TYPE-UNIDENTIFIED))))
