# Reglas de trabajo para esta app

## Deploys (pedido del dueño del proyecto)

- Cada push a la rama dispara un deploy, así que se mandan **pocos deploys
  grandes**, no muchos chicos.
- Juntar todo el trabajo de una tanda en **un solo commit** y hacer **un solo
  push** al final (si hubo commits locales intermedios, juntarlos en uno antes
  de subir).
- Numerarlos: el mensaje del commit empieza con `DP #N:` y el número sigue al
  último que ya está en la rama (`git log --oneline | grep "DP #"`). El primero
  es `DP #1`.
- El cuerpo del commit lista todo lo que trae ese deploy.
