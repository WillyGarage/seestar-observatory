"""Stack -> finished picture, from a Seestar .fit master.
Normalisation and noise stats come from the FULL frame; cropping happens last,
so a tight crop on a bright target doesn't clip the target itself."""
import numpy as np, cv2, sys, json

def read_fit(p):
    b=open(p,'rb').read(); i=0; hdr={}
    while True:
        c=b[i:i+80].decode('latin1'); i+=80
        k=c[:8].strip()
        if k=='END': break
        if '=' in c: hdr[k]=c.split('=',1)[1].split('/')[0].strip().strip("'").strip()
    i=((i+2879)//2880)*2880
    w,h,n=int(hdr['NAXIS1']),int(hdr['NAXIS2']),int(hdr['NAXIS3'])
    a=np.frombuffer(b[i:i+w*h*n*2],dtype='>i2').astype(np.float32)+float(hdr.get('BZERO',0))
    return a.reshape((n,h,w)).transpose(1,2,0), hdr

def process(rgb, a=0.001, sat=1.5, h=3, hcolor=20, crop=None):
    x = rgb.astype(np.float32)
    bg = np.stack([cv2.GaussianBlur(x[...,c],(0,0),160) for c in range(3)],-1)
    x = x - bg
    sig = np.std(x[x < np.percentile(x, 70)])
    x = np.clip(x - 0.7*sig, 0, None)
    x /= np.percentile(x, 99.999)          # brightest stars, full frame
    y = np.clip(np.arcsinh(x/a)/np.arcsinh(1.0/a), 0, 1)
    u8 = (y*255).astype(np.uint8)
    if crop:
        y0,y1,x0,x1 = crop; u8 = u8[y0:y1, x0:x1]
    u8 = cv2.fastNlMeansDenoisingColored(u8, None, h, hcolor, 7, 21)
    z = u8.astype(np.float32)/255.
    L = z.mean(-1, keepdims=True)
    w = np.clip((L-0.10)/0.30, 0, 1)
    z = np.clip(L + (z-L)*(1+(sat-1)*w), 0, 1)
    return (z*255).astype(np.uint8)

if __name__ == "__main__":
    src,out,crop = sys.argv[1],sys.argv[2],sys.argv[3]
    a = float(sys.argv[4]) if len(sys.argv)>4 else 0.001
    rgb,hdr = read_fit(src)
    cr = None if crop=='full' else tuple(int(v) for v in crop.split(','))
    img = process(rgb, a, crop=cr)
    cv2.imwrite(out, cv2.cvtColor(img,cv2.COLOR_RGB2BGR),[cv2.IMWRITE_JPEG_QUALITY,95])
    h_,w_=img.shape[:2]
    cv2.imwrite(out.replace('.jpg','_view.jpg'), cv2.resize(cv2.cvtColor(img,cv2.COLOR_RGB2BGR),(540,int(h_*540/w_))))
    print(hdr['OBJECT'],hdr['STACKCNT'],'->',out,img.shape)
